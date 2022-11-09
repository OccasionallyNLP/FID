# -*- coding: utf-8 -*-

import torch
import torch.nn as nn
import transformers
import types
import torch.nn.functional as F
from torch.nn import CrossEntropyLoss
import numpy as np

# huggingface의 T5를 상속해서 활용
class FiDT5(transformers.T5ForConditionalGeneration):
    def __init__(self, config):
        super().__init__(config)
        # FiDT5의 핵심 포인트인 encoder에서 decoder 넘어가는 부분을 변경하는 부분
        self.wrap_encoder()

    def forward_(self, **kwargs):
        # input_ids - (bs, n, seq_len) 
        # bs - batch size, n - retrieved된 passage의 개수, seq_len : passage의 길이
        # 기본적으로 T5의 input으로는 input_ids, attention_mask, decoder_input_ids, decoder_attention_mask가 들어감. 
        # input_ids는 encoder의 input임.
        # input_ids - (bs, n, seq_len) -> (bs, n*seq_len)으로 shape 변경
        if 'input_ids' in kwargs:
            kwargs['input_ids'] = kwargs['input_ids'].view(kwargs['input_ids'].size(0), -1)
        if 'attention_mask' in kwargs:
            kwargs['attention_mask'] = kwargs['attention_mask'].view(kwargs['attention_mask'].size(0), -1)
        
        return super(FiDT5, self).forward(
            **kwargs
        )

    # input ids : bs, n, seq_len -> bs, n*seq_len
    # We need to resize as B x (N * L) instead of (B * N) x L here
    # because the T5 forward method uses the input tensors to infer
    # dimensions used in the decoder.
    # EncoderWrapper resizes the inputs as (B * N) x L.
    def forward(self, input_ids=None, attention_mask=None, **kwargs):
        if input_ids != None:
            # (bs, n, seq_len) -> (bs, n*seq_len) 
            # inputs might have already be resized in the generate method
            if input_ids.dim() == 3:
                self.encoder.n_passages = input_ids.size(1)
                input_ids = input_ids.view(input_ids.size(0), -1)
        if attention_mask != None:
            attention_mask = attention_mask.view(attention_mask.size(0), -1)
        #print(input_ids.shape)
        return super().forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            **kwargs
        )

    def generate(self, input_ids, attention_mask, **kwargs):
        # input ids - bs, n, seq_len -> bs, n*seq_len
        self.encoder.n_passages = input_ids.size(1)
        # default generate function 활용 -> default 값에 대한 이해가 필요.
        return super().generate(
            input_ids=input_ids.view(input_ids.size(0), -1),
            attention_mask=attention_mask.view(attention_mask.size(0), -1),
            **kwargs,
        )
    # 기존 T5와 다르게 encoder만을 다르게 만드는 부분.
    def wrap_encoder(self, use_checkpoint=False):
        """
        Wrap T5 encoder to obtain a Fusion-in-Decoder model.
        """
        self.encoder = EncoderWrapper(self.encoder)
    
    # t5 weight를 불러오기 위해서 필요한, T5처럼 만드는 부분
    def unwrap_encoder(self):
        """
        Unwrap Fusion-in-Decoder encoder, useful to load T5 weights.
        """
        self.encoder = self.encoder.encoder
        block = []
        for mod in self.encoder.block:
            block.append(mod.module)
        block = nn.ModuleList(block)
        self.encoder.block = block
    
    def load_t5(self, state_dict):
        self.unwrap_encoder()# //->Fid->T5
        self.load_state_dict(state_dict) # paramerter loading
        self.wrap_encoder() # T5->FiDT5로. 

    def set_checkpoint(self, use_checkpoint):
        """
        Enable or disable checkpointing in the encoder.
        See https://pytorch.org/docs/stable/checkpoint.html
        """
        for mod in self.encoder.encoder.block:
            mod.use_checkpoint = use_checkpoint

class CheckpointWrapper(torch.nn.Module):
    """
    Wrapper replacing None outputs by empty tensors, which allows the use of
    checkpointing.
    """
    def __init__(self, module, use_checkpoint=False):
        super().__init__()
        self.module = module
        self.use_checkpoint = use_checkpoint

    def forward(self, hidden_states, attention_mask, position_bias, **kwargs):
        if self.use_checkpoint and self.training:
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            def custom_forward(*inputs):
                output = self.module(*inputs, **kwargs)
                empty = torch.tensor(
                    [],
                    dtype=torch.float,
                    device=output[0].device,
                    requires_grad=True)
                output = tuple(x if x is not None else empty for x in output)
                return output

            output = torch.utils.checkpoint.checkpoint(
                custom_forward,
                hidden_states,
                attention_mask,
                position_bias
            )
            output = tuple(x if x.size() != 0 else None for x in output)
        else:
            output = self.module(hidden_states, attention_mask, position_bias, **kwargs)
        return output

def apply_checkpoint_wrapper(t5stack, use_checkpoint):
    """
    Wrap each block of the encoder to enable checkpointing.
    """
    block = []
    for mod in t5stack.block:
        wrapped_mod = CheckpointWrapper(mod, use_checkpoint)
        block.append(wrapped_mod)
    block = nn.ModuleList(block)
    t5stack.block = block

    
class FiDBart(transformers.BartForConditionalGeneration):
    def __init__(self, config):
        super().__init__(config)
        self.wrap_encoder()        
    
    def forward(self, input_ids=None, attention_mask=None, **kwargs):
        
        if input_ids != None:
            # (bs, n, seq_len) -> (bs, n*seq_len) 
            # inputs might have already be resized in the generate method
            if input_ids.dim() == 3:
                self.model.encoder.n_passages = input_ids.size(1)
                input_ids = input_ids.view(input_ids.size(0), -1)

        if attention_mask != None:
            attention_mask = attention_mask.view(attention_mask.size(0), -1) 
        
        return super().forward(input_ids=input_ids, attention_mask=attention_mask, **kwargs)

    def generate(self, input_ids, attention_mask, **kwargs):
        # input ids - bs, n, seq_len -> bs, n*seq_len
        self.model.encoder.n_passages = input_ids.size(1)
        # default generate function 활용 -> default 값에 대한 이해가 필요.
        return super().generate(
            input_ids=input_ids.view(input_ids.size(0), -1),
            attention_mask=attention_mask.view(attention_mask.size(0),-1),
            **kwargs)

    def wrap_encoder(self):
        """
        Wrap T5 encoder to obtain a Fusion-in-Decoder model.
        """
        self.model.encoder = EncoderWrapper(self.model.encoder)
        
    def unwrap_encoder(self):
        """
        Unwrap Fusion-in-Decoder encoder, useful to load bart weights.
        """
        self.model.encoder = self.model.encoder.encoder
        block = []
        for mod in self.model.encoder.layers:
            block.append(mod)
        block = nn.ModuleList(block)
        self.model.encoder.layers = block
        
    def load_pretrained_model(self, state_dict):
        self.unwrap_encoder()# //->Fid->Bart
        self.load_state_dict(state_dict) # paramerter loading
        self.wrap_encoder() # T5->FiDT5로.         

class EncoderWrapper(torch.nn.Module):
    def __init__(self, encoder):
        super().__init__()
        # encoder 저장해둠
        self.encoder = encoder
        try:
            self.main_input_name = encoder.main_input_name
        except:
            pass
    
    def forward(self, input_ids=None, attention_mask=None,**kwargs):
        bsz, total_length = input_ids.shape
        passage_length = total_length // self.n_passages
        # total_input
        input_ids = input_ids.view(bsz*self.n_passages, passage_length)
        attention_mask = attention_mask.view(bsz*self.n_passages, passage_length)
        outputs = self.encoder(input_ids=input_ids, attention_mask=attention_mask, **kwargs) # encoder_output : ( bs, n*seq_len, dim )
        outputs.last_hidden_state = outputs.last_hidden_state.view(bsz, self.n_passages*passage_length, -1)
        return outputs 

