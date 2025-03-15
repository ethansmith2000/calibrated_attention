import torch
from einops import rearrange
from torch import nn
import torch.nn.functional as F
from torch.nn.functional import scaled_dot_product_attention as sdpa
import math

def log_base(x, base):
    """Calculate logarithm of x with custom base."""
    return torch.log(x) / torch.log(torch.tensor(base, dtype=x.dtype, device=x.device))

def adaptive_temperature_softmax(logits):
    """
    scaling approach from softmax is not enough (for sharp out-of-distribution)
    """
    original_probs = torch.nn.functional.softmax(logits, dim=-1)
    poly_fit = torch.tensor([-0.037, 0.481, -2.3, 4.917, -1.791]) # see Figure 5
    entropy = torch.sum(-original_probs * torch.log(original_probs + 1e-9),
                       dim=-1, keepdim=True) # compute the Shannon entropy
    
    # PyTorch doesn't have polyval, so we implement the polynomial evaluation directly
    poly_result = poly_fit[0] * entropy.pow(4) + poly_fit[1] * entropy.pow(3) + poly_fit[2] * entropy.pow(2) + poly_fit[3] * entropy + poly_fit[4]
    
    beta = torch.where( # beta = 1 / theta
        entropy > 0.5, # don't overcorrect low-entropy heads
        torch.maximum(poly_result, torch.tensor(1.0)), # never increase entropy
        torch.tensor(1.0))
    
    return torch.nn.functional.softmax(logits * beta, dim=-1)


def yarn_scaling(seq_len, head_dim):
    """
    yarn scaling from YaRN: Efficient Context Window Extension of Large Language Models
    """
    return ((0.1 * torch.log(seq_len[None,None,:,None]) + 1)**2) / (head_dim**0.5)


def relative_scaling(seq_len, head_dim, base_seq_len=2048):
    """
    Training-free Diffusion Model Adaptation for Variable-Sized Text-to-Image Synthesis
    """
    return (log_base(seq_len[None,None,:,None], base_seq_len) / head_dim) ** 0.5


def relative_scaling_2(seq_len, head_dim, attn_bias, base_seq_len=2048):
    """
    Pippo: High-Resolution Multi-View Humans from a Single Image
    """
    scale = (((log_base(seq_len[None,None,:,None], base_seq_len) * attn_bias) / head_dim)) ** 0.5   
    return scale 


class AttentionBase(nn.Module):
    """
    Causal multihead attention that uses torch's SDPA 
    """
    def __init__(self, dim=512, heads=8):
        super().__init__()
        self.heads = heads
        self.to_q, self.to_k, self.to_v, self.to_out = map(lambda i: nn.Linear(dim, dim, bias=i == 3), range(4))

    def forward(self, x):
        b, n, d, h = (*x.shape, self.heads)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (self.to_q(x), self.to_k(x), self.to_v(x)))
        out = sdpa(q, k, v, is_causal=True)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)
    


class AttentionRelativeScaling1(AttentionBase):
    """
    uses the first relative scaling function
    presently only set up for causal attention where q, k, v all have the same shape, 
    so kv cached inference not yet supported
    """
    def __init__(self, dim=512, heads=8, base_seq_len=2048):
        super().__init__(dim, heads)
        self.base_seq_len = base_seq_len

    def forward(self, x):
        b, n, d, h = (*x.shape, self.heads)

        # get scales (n,)
        seq_lens = torch.arange(n, device=x.device, dtype=x.dtype)
        scales = relative_scaling(seq_lens, d / self.heads, self.base_seq_len) # (1, 1, n, 1)

        # instead of applying scale to QK matrix, we can just mult the queries (or keys) its all linear!
        q, k, v = self.to_q(x), self.to_k(x), self.to_v(x)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (q, k, v))
        q = q * scales

        out = sdpa(q, k, v, is_causal=True, scale=1.0) # we've already done our scaling , so set to 1.0
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)



class AttentionRelativeScaling2(AttentionBase):
    """
    uses the relative scaling function from PIPPO

    presently only set up for causal attention where q, k, v all have the same shape, 
    so kv cached inference not yet supported
    """
    def __init__(self, dim=512, heads=8, base_seq_len=2048, attn_bias=1.5, learned_bias=False):
        super().__init__(dim, heads)
        self.base_seq_len = base_seq_len
        attn_bias = torch.full((1, heads, 1, 1), attn_bias)
        if learned_bias:
            self.register_parameter('attn_bias', nn.Parameter(attn_bias))
        else:
            self.register_buffer('attn_bias', attn_bias)

    def forward(self, x):
        b, n, d, h = (*x.shape, self.heads)

        # get scales (n,)
        seq_lens = torch.arange(n, device=x.device, dtype=x.dtype)
        scales = relative_scaling_2(seq_lens, d / self.heads, self.attn_bias, self.base_seq_len) # (1, h, n, 1)

        # instead of applying scale to QK matrix, we can just mult the queries (or keys) its all linear!
        q, k, v = self.to_q(x), self.to_k(x), self.to_v(x)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (q, k, v))
        q = q * scales
        
        out = sdpa(q, k, v, is_causal=True, scale=1.0) # we've already done our scaling , so set to 1.0
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)


class AttentionYarnScaling(AttentionBase):
    """
    uses yarn scaling from YaRN: Efficient Context Window Extension of Large Language Models

    presently only set up for causal attention where q, k, v all have the same shape, 
    so kv cached inference not yet supported
    """
    def forward(self, x):
        b, n, d, h = (*x.shape, self.heads)

        # get scales (n,)
        seq_lens = torch.arange(n, device=x.device, dtype=x.dtype)
        scales = yarn_scaling(seq_lens, d / self.heads)

        # instead of applying scale to QK matrix, we can just mult the queries (or keys) its all linear!
        q, k, v = self.to_q(x), self.to_k(x), self.to_v(x)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (q, k, v))
        q = q * scales

        out = sdpa(q, k, v, is_causal=True, scale=1.0) # we've already done our scaling , so set to 1.0
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)


class AttentionPolyFitScaling(AttentionBase):
    """
    uses scaling from softmax is not enough (for sharp out-of-distribution)

    presently only set up for causal attention where q, k, v all have the same shape, 
    so kv cached inference not yet supported
    """
    def forward(self, x):
        b, n, d, h = (*x.shape, self.heads)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (self.to_q(x), self.to_k(x), self.to_v(x)))

        # compute attention scores
        sim = torch.einsum('b h i d, b h j d -> b h i j', q, k)
        
        # apply scaling factor
        sim = sim / math.sqrt(q.shape[-1])
        
        # apply causal mask
        mask = torch.ones((n, n), device=x.device, dtype=torch.bool).triu(1)
        sim = sim.masked_fill(mask, -torch.finfo(sim.dtype).max)
        
        # apply softmax
        attn = adaptive_temperature_softmax(sim)
        
        # apply attention to values
        out =torch. einsum('b h i j, b h j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)


class AttentionLearnedScaling(AttentionBase):
    """
    A learned function to provide sequence length dependent scaling, per head


    presently only set up for causal attention where q, k, v all have the same shape, 
    so kv cached inference not yet supported
    """
    def __init__(self, dim=512, heads=8):
        super().__init__(dim, heads)
        self.alphas = nn.Parameter(torch.zeros(1, heads, 1, 1)) # favor negative values, denominator should shrink with larger sequences
        self.betas = nn.Parameter(torch.zeros(1, heads, 1, 1))
        self.head_dim = dim / heads
        self.base_scale = self.head_dim ** 0.5

    # def create_scaling(self, seq_lens):
    #     """
    #     scales are computed as a residual to the base scaling:
    #     base_scale = head_dim ** 0.5
    #     scales = (alphas * log(seq_len) + betas) + base_scale
    #     """
    #     log_seq_lens = torch.log(seq_lens[None,None,:,None])
    #     learned_scale = self.alphas * log_seq_lens + self.betas
    #     scales = learned_scale + self.base_scale
    #     scales = 1 / scales
    #     return scales

    def create_scaling(self, seq_lens):
        """
        scales are computed as a multiplier to the base scaling:
        base_scale = head_dim ** 0.5
        scales = (alphas * log(seq_len) + betas) + base_scale
        """
        log_seq_lens = torch.log(seq_lens[None,None,:,None])
        multiplier = 1 + self.alphas * log_seq_lens + self.betas
        scales = multiplier * self.base_scale
        scales = 1 / scales
        return scales

    def forward(self, x):
        b, n, d, h = (*x.shape, self.heads)

        # get scales (n,)
        seq_lens = torch.arange(n, device=x.device, dtype=x.dtype)
        scales = self.create_scaling(seq_lens)

        # instead of applying scale to QK matrix, we can just mult the queries (or keys) its all linear!
        q, k, v = self.to_q(x), self.to_k(x), self.to_v(x)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (q, k, v))
        q = q * scales

        out = sdpa(q, k, v, is_causal=True, scale=1.0) # we've already done our scaling , so set to 1.0
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)


class AttentionSoftmaxPlusOne(AttentionBase):
    """
    Inspired by softmax plus one, but instead learning the constant

    presently only set up for causal attention where q, k, v all have the same shape, 
    so kv cached inference not yet supported

    in desperate need of at least a softmax kernel if not fully fused attention
    """
    def __init__(self, dim=512, heads=8):
        super().__init__(dim, heads)
        self.denom_bias = nn.Parameter(torch.zeros(1, heads, 1, 1))

    def forward(self, x):
        b, n, d, h = (*x.shape, self.heads)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (self.to_q(x), self.to_k(x), self.to_v(x)))

        # compute attention scores
        sim = torch.einsum('b h i d, b h j d -> b h i j', q, k)
        
        # apply scaling factor
        sim = sim / math.sqrt(q.shape[-1])
        
        # apply causal mask
        mask = torch.ones((n, n), device=x.device, dtype=torch.bool).triu(1)
        sim = sim.masked_fill(mask, -torch.finfo(sim.dtype).max)
        
        # apply softmax
        sim_max = torch.max(sim, dim=-1, keepdim=True)[0]
        exp_sim = torch.exp(sim - sim_max)
        
        # compute denominator (sum along dim=-1)
        # we'll modify this with learned parameters
        denom = exp_sim.sum(dim=-1, keepdim=True)
        
        # apply alpha and beta parameters to modify the denominator
        modified_denom = denom + torch.exp(self.denom_bias)
        
        # compute attention weights
        attn = exp_sim / modified_denom
        
        # apply attention to values
        out = torch.einsum('b h i j, b h j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)


class AttentionSoftmaxPlusFN(AttentionBase):
    """
    Inspired by softmax plus one, but instead letting the denominator be a learned function of sequence length

    presently only set up for causal attention where q, k, v all have the same shape, 
    so kv cached inference not yet supported

    in desperate need of at least a softmax kernel if not fully fused attention
    """
    def __init__(self, dim=512, heads=8):
        super().__init__(dim, heads)
        self.alphas = nn.Parameter(torch.zeros(1, heads, 1, 1))
        self.betas = nn.Parameter(torch.zeros(1, heads, 1, 1))

    def forward(self, x):
        b, n, d, h = (*x.shape, self.heads)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=h), (self.to_q(x), self.to_k(x), self.to_v(x)))

        # compute attention scores
        sim = torch.einsum('b h i d, b h j d -> b h i j', q, k)
        
        # apply scaling factor
        sim = sim / math.sqrt(q.shape[-1])
        
        # apply causal mask
        mask = torch.ones((n, n), device=x.device, dtype=torch.bool).triu(1)
        sim = sim.masked_fill(mask, -torch.finfo(sim.dtype).max)
        
        # apply softmax
        sim_max = torch.max(sim, dim=-1, keepdim=True)[0]
        exp_sim = torch.exp(sim - sim_max)
        
        # compute denominator (sum along dim=-1)
        # we'll modify this with learned parameters
        denom = exp_sim.sum(dim=-1, keepdim=True)
        
        # apply alpha and beta parameters to modify the denominator
        seq_lens = torch.arange(n, device=x.device, dtype=x.dtype)
        modified_denom = denom + torch.exp(self.alphas * torch.log(seq_lens[None,None,:,None]) + self.betas)
        
        # compute attention weights
        attn = exp_sim / modified_denom
        
        # apply attention to values
        out = torch.einsum('b h i j, b h j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)