from dataclasses import dataclass
import torch
import torch.nn as nn
import torchtune

# Import the gemma function from the component builders
try:
    from torchtune.models.gemma._component_builders import gemma
except ImportError:
    gemma = None
    print("⚠️ 'gemma' function not found in torchtune. Please ensure it's installed or available.")

#####################################################
# 1. Gemma 12B definition
#####################################################
def gemma12B() -> torchtune.modules.transformer.TransformerDecoder:
    """
    Instantiates a Gemma 12B model with specified hyperparameters.
    """
    if gemma is None:
        raise ImportError("'gemma' function is not available in torchtune. Please install or add it.")
    
    return gemma(
        vocab_size=256_000,       # Adjust to match your actual vocab size
        num_layers=36,            # Example: 36 layers for ~12B model
        num_heads=48,             # Example: 48 attention heads
        head_dim=256,             # Dimension per head
        num_kv_heads=1,           # Number of key-value heads
        embed_dim=12_288,         # Embedding dimension
        intermediate_dim=49_152,  # Intermediate dimension (typically 4x embed_dim)
        max_seq_len=8192,         # Maximum sequence length
        attn_dropout=0.0,         # Attention dropout
        norm_eps=1e-6,            # Normalization epsilon
    )

#####################################################
# 2. FLAVORS dictionary
#####################################################
FLAVORS = {
    "gemma-12B": gemma12B,
}



#####################################################
# 3. Utility / Shared Code
#####################################################
def _prepare_transformer(model):
    embed_dim = model.tok_embeddings.embedding_dim
    model.tok_embeddings = nn.Identity()
    model.output = nn.Identity()
    return model, embed_dim

def _create_causal_mask(seq_len: int, device: torch.device):
    return torch.tril(torch.ones(seq_len, seq_len, dtype=torch.bool, device=device))

def _index_causal_mask(mask: torch.Tensor, input_pos: torch.Tensor):
    """
    Args:
        mask: (max_seq_len, max_seq_len)
        input_pos: (batch_size, seq_len)

    Returns:
        (batch_size, seq_len, max_seq_len)
    """
    r = mask[input_pos, :]
    return r

def _multinomial_sample_one_no_sync(probs):  # Does multinomial sampling without a cuda synchronization
    q = torch.empty_like(probs).exponential_(1)
    return torch.argmax(probs / q, dim=-1, keepdim=True).to(dtype=torch.int)

def sample_topk(logits: torch.Tensor, topk: int, temperature: float):
    logits = logits / temperature
    filter_value: float = -float("Inf")
    indices_to_remove = logits < torch.topk(logits, topk)[0][..., -1, None]
    scores_processed = logits.masked_fill(indices_to_remove, filter_value)
    scores_processed = torch.nn.functional.log_softmax(scores_processed, dim=-1)
    probs = torch.nn.functional.softmax(scores_processed, dim=-1)

    sample_token = _multinomial_sample_one_no_sync(probs)
    return sample_token


@dataclass
class ModelArgs:
    backbone_flavor: str      # e.g. "gemma-12B"
    decoder_flavor: str       # e.g. "gemma-12B"
    text_vocab_size: int
    audio_vocab_size: int
    audio_num_codebooks: int


#####################################################
# 4. The main Model class (unchanged)
#####################################################
class Model(nn.Module):
    def __init__(self, args: ModelArgs):
        super().__init__()
        self.args = args

        # Build the backbone and decoder from Gemma only
        self.backbone, backbone_dim = _prepare_transformer(FLAVORS[args.backbone_flavor]())
        self.decoder, decoder_dim   = _prepare_transformer(FLAVORS[args.decoder_flavor]())

        self.text_embeddings  = nn.Embedding(args.text_vocab_size, backbone_dim)
        self.audio_embeddings = nn.Embedding(args.audio_vocab_size * args.audio_num_codebooks, backbone_dim)

        self.projection    = nn.Linear(backbone_dim, decoder_dim, bias=False)
        self.codebook0_head = nn.Linear(backbone_dim, args.audio_vocab_size, bias=False)
        self.audio_head     = nn.Parameter(torch.empty(args.audio_num_codebooks - 1, decoder_dim, args.audio_vocab_size))

    def setup_caches(self, max_batch_size: int) -> torch.Tensor:
        """Setup KV caches and return a causal mask."""
        dtype  = next(self.parameters()).dtype
        device = next(self.parameters()).device

        with device:
            self.backbone.setup_caches(max_batch_size, dtype)
            self.decoder.setup_caches(max_batch_size, dtype, decoder_max_seq_len=self.args.audio_num_codebooks)

        self.register_buffer("backbone_causal_mask", _create_causal_mask(self.backbone.max_seq_len, device))
        self.register_buffer("decoder_causal_mask", _create_causal_mask(self.args.audio_num_codebooks, device))

    @torch.inference_mode()
    def generate_frames_batch(self, tokens, tokens_mask, pos, temperature=0.9, topk=50, num_frames=16):
        """Generate multiple audio frames at once to improve GPU utilization.
        
        Args:
            tokens: Input tokens tensor
            tokens_mask: Input tokens mask tensor
            pos: Position tensor
            temperature: Sampling temperature
            topk: Top-k sampling parameter
            num_frames: Number of frames to generate in this batch
            
        Returns:
            Tensor containing generated frames, shape (num_frames, codebook_size)
        """
        try:
            # Initialize storage for batch samples
            batch_samples = []
            
            # Make deep copies to avoid modifying the original tensors
            curr_tokens = tokens.clone()
            curr_tokens_mask = tokens_mask.clone()
            curr_pos = pos.clone()
            
            # Generate frames one by one, but prepare them as a batch
            for i in range(num_frames):
                # Generate a single frame
                logits = self.forward(curr_tokens, curr_tokens_mask, curr_pos)
                
                # Apply temperature
                if temperature > 0:
                    logits = logits / temperature
                
                # Apply top-k if specified
                if topk > 0:
                    v, _ = torch.topk(logits, topk)
                    logits[logits < v[:, [-1]]] = -float("Inf")
                
                # Sample from the logits
                probs = torch.softmax(logits, dim=-1)
                sample = torch.multinomial(probs, num_samples=1)
                
                # Check for end condition
                if torch.all(sample == 0):
                    break
                    
                batch_samples.append(sample)
                
                # Update for next iteration
                curr_tokens = torch.cat([sample, torch.zeros(1, 1).long().to(sample.device)], dim=1)
                curr_tokens_mask = torch.cat(
                    [torch.ones_like(sample).bool(), torch.zeros(1, 1).bool().to(sample.device)], dim=1
                )
                curr_pos = curr_pos[:, -1:] + 1
            
            if not batch_samples:
                return None
                
            # Stack all generated samples
            return torch.cat(batch_samples, dim=0)
        
        except Exception as e:
            print(f"Error in generate_frames_batch: {e}")
            return None
            
    def generate_frame(
        self,
        tokens: torch.Tensor,
        tokens_mask: torch.Tensor,
        input_pos: torch.Tensor,
        temperature: float,
        topk: int,
    ) -> torch.Tensor:
        """
        Args:
            tokens: (batch_size, seq_len, audio_num_codebooks+1)
            tokens_mask: (batch_size, seq_len, audio_num_codebooks+1)
            input_pos: (batch_size, seq_len) positions for each token
            mask: (batch_size, seq_len, max_seq_len)

        Returns:
            (batch_size, audio_num_codebooks) sampled tokens
        """
        dtype = next(self.parameters()).dtype
        b, s, _ = tokens.size()

        assert self.backbone.caches_are_enabled(), "backbone caches are not enabled"
        curr_backbone_mask = _index_causal_mask(self.backbone_causal_mask, input_pos)

        # Embed tokens
        embeds = self._embed_tokens(tokens)
        masked_embeds = embeds * tokens_mask.unsqueeze(-1)

        # Summation across codebooks + text
        h = masked_embeds.sum(dim=2)
        h = self.backbone(h, input_pos=input_pos, mask=curr_backbone_mask).to(dtype=dtype)

        last_h = h[:, -1, :]
        c0_logits = self.codebook0_head(last_h)
        c0_sample = sample_topk(c0_logits, topk, temperature)
        c0_embed = self._embed_audio(0, c0_sample)

        # Merge last hidden state with codebook0
        curr_h     = torch.cat([last_h.unsqueeze(1), c0_embed], dim=1)
        curr_sample = c0_sample.clone()
        curr_pos    = torch.arange(0, curr_h.size(1), device=curr_h.device).unsqueeze(0).repeat(curr_h.size(0), 1)

        # Reset decoder caches for each frame
        self.decoder.reset_caches()
        for i in range(1, self.args.audio_num_codebooks):
            curr_decoder_mask = _index_causal_mask(self.decoder_causal_mask, curr_pos)
            decoder_h = self.decoder(self.projection(curr_h), input_pos=curr_pos, mask=curr_decoder_mask).to(dtype=dtype)

            ci_logits = torch.mm(decoder_h[:, -1, :], self.audio_head[i - 1])
            ci_sample = sample_topk(ci_logits, topk, temperature)
            ci_embed  = self._embed_audio(i, ci_sample)

            curr_h     = ci_embed
            curr_sample = torch.cat([curr_sample, ci_sample], dim=1)
            curr_pos    = curr_pos[:, -1:] + 1

        return curr_sample

    def reset_caches(self):
        self.backbone.reset_caches()
        self.decoder.reset_caches()

    def _embed_audio(self, codebook: int, tokens: torch.Tensor) -> torch.Tensor:
        return self.audio_embeddings(tokens + codebook * self.args.audio_vocab_size)

    def _embed_tokens(self, tokens: torch.Tensor) -> torch.Tensor:
        text_embeds = self.text_embeddings(tokens[:, :, -1]).unsqueeze(-2)

        audio_tokens = tokens[:, :, :-1] + (
            self.args.audio_vocab_size * torch.arange(self.args.audio_num_codebooks, device=tokens.device)
        )
        audio_embeds = self.audio_embeddings(audio_tokens.view(-1)).reshape(
            tokens.size(0), tokens.size(1), self.args.audio_num_codebooks, -1
        )

        return torch.cat([audio_embeds, text_embeds], dim=-2)

    def forward(self, tokens, tokens_mask, input_pos):
        """
        Forward method needed for the generate_frames_batch method.
        This should leverage the existing generation code to produce logits.
        """
        dtype = next(self.parameters()).dtype
        
        # Similar to generate_frame but returns logits instead of sampling
        assert self.backbone.caches_are_enabled(), "backbone caches are not enabled"
        curr_backbone_mask = _index_causal_mask(self.backbone_causal_mask, input_pos)
        
        # Embed tokens
        embeds = self._embed_tokens(tokens)
        masked_embeds = embeds * tokens_mask.unsqueeze(-1)
        
        # Summation across codebooks + text
        h = masked_embeds.sum(dim=2)
        h = self.backbone(h, input_pos=input_pos, mask=curr_backbone_mask).to(dtype=dtype)
        
        last_h = h[:, -1, :]
        c0_logits = self.codebook0_head(last_h)
        
        return c0_logits
