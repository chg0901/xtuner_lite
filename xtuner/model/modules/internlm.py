# Copyright (c) OpenMMLab. All rights reserved.
from typing import Optional, Tuple

import torch
import torch.distributed as dist
import torch.nn.functional as F
from mmengine import MessageHub

SUPPORT_XFORMERS = False
SUPPORT_FLASH2 = False
try:
    import xformers.ops as xops

    SUPPORT_XFORMERS = True
except ImportError:
    pass

try:
    from flash_attn import flash_attn_func, flash_attn_varlen_func

    SUPPORT_FLASH2 = True
except ImportError:
    pass


def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., :x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2:]
    return torch.cat((-x2, x1), dim=-1)


def apply_rotary_pos_emb(q, k, cos, sin, position_ids):
    # The first two dimensions of cos and sin are always 1, so we can
    # `squeeze` them.
    cos = cos.squeeze(1).squeeze(0)  # [seq_len, dim]
    sin = sin.squeeze(1).squeeze(0)  # [seq_len, dim]
    cos = cos[position_ids].unsqueeze(1)  # [bs, 1, seq_len, dim]
    sin = sin[position_ids].unsqueeze(1)  # [bs, 1, seq_len, dim]
    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)
    return q_embed, k_embed


def internlm_attn_forward(
    self,
    hidden_states: torch.Tensor,
    attention_mask: Optional[torch.Tensor] = None,
    position_ids: Optional[torch.LongTensor] = None,
    past_key_value: Optional[Tuple[torch.Tensor]] = None,
    output_attentions: bool = False,
    use_cache: bool = False,
) -> Tuple[torch.Tensor, Optional[torch.Tensor],
           Optional[Tuple[torch.Tensor]]]:

    message_hub = MessageHub.get_instance('for_flash_attn')
    rank = dist.get_rank()
    cumulative_len = message_hub.get_info(f'cumulative_len_rank_{rank}')
    indexes = message_hub.get_info(f'indexes_rank_{rank}')
    max_seqlen = message_hub.get_info(f'max_seqlen_rank_{rank}')
    use_local_attn = cumulative_len is not None
    # Modified from https://huggingface.co/internlm/internlm-7b/blob/939a68c0dc1bd5f35b63c87d44af05ce33379061/modeling_internlm.py#L161  # noqa:E501
    bsz, q_len, _ = hidden_states.size()

    if use_local_attn:
        assert len(cumulative_len) == bsz and cumulative_len[0][-1] == q_len

    query_states = self.q_proj(hidden_states).view(bsz, q_len, self.num_heads,
                                                   self.head_dim).transpose(
                                                       1, 2)
    key_states = self.k_proj(hidden_states).view(bsz, q_len, self.num_heads,
                                                 self.head_dim).transpose(
                                                     1, 2)
    value_states = self.v_proj(hidden_states).view(bsz, q_len, self.num_heads,
                                                   self.head_dim).transpose(
                                                       1, 2)

    kv_seq_len = key_states.shape[-2]
    if past_key_value is not None:
        kv_seq_len += past_key_value[0].shape[-2]
    cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
    query_states, key_states = apply_rotary_pos_emb(
        query_states, key_states, cos, sin,
        indexes if use_local_attn else position_ids)
    # [bsz, nh, t, hd]

    if past_key_value is not None:
        # reuse k, v, self_attention
        key_states = torch.cat([past_key_value[0], key_states], dim=2)
        value_states = torch.cat([past_key_value[1], value_states], dim=2)

    past_key_value = (key_states, value_states) if use_cache else None

    assert SUPPORT_FLASH2
    if SUPPORT_FLASH2 or SUPPORT_XFORMERS:
        # q, k, v is [B, H, S, K] and xformers need [B, S, H, K].
        # returns [B, S, H, K]
        query_states = query_states.transpose(1, 2)
        key_states = key_states.transpose(1, 2)
        value_states = value_states.transpose(1, 2)
        if SUPPORT_FLASH2:
            if use_local_attn:
                q_unpad, k_unpad, v_unpad = query_states.flatten(
                    0, 1), key_states.flatten(0,
                                              1), value_states.flatten(0, 1)
                for i in range(1, bsz):
                    cumulative_len[i] += q_len * i
                cumulative_len = torch.cat(cumulative_len, dim=0)
                attn_output = flash_attn_varlen_func(
                    q_unpad,
                    k_unpad,
                    v_unpad,
                    cumulative_len,
                    cumulative_len,
                    max_seqlen,
                    max_seqlen,
                    0,
                    return_attn_probs=False,
                    causal=True,
                )
            else:
                attn_output = flash_attn_func(
                    query_states, key_states, value_states, causal=True)
        else:
            attn_output = xops.memory_efficient_attention(
                query_states,
                key_states,
                value_states,
                attn_bias=xops.LowerTriangularMask())
    else:
        attn_output = F.scaled_dot_product_attention(
            query_states, key_states, value_states, attn_mask=attention_mask)
        attn_output = attn_output.transpose(1, 2)

    attn_output = attn_output.reshape(bsz, q_len, self.hidden_size)

    attn_output = self.o_proj(attn_output)

    # Due to the implementation of the PyTorch version of flash attention,
    # even when the output_attentions flag is set to True, it is not possible
    # to return the attn_weights.
    return attn_output, None, past_key_value
