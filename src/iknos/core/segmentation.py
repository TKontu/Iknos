import torch
import torch.nn.functional as F
import re

def calculate_adjacent_similarities(embeddings: list[list[float]]) -> list[float]:
    """
    Calculate cosine similarity between adjacent embeddings.
    """
    if len(embeddings) < 2:
        return []
        
    tensor_embeddings = torch.tensor(embeddings, dtype=torch.float32)
    # Normalize embeddings
    tensor_embeddings = F.normalize(tensor_embeddings, p=2, dim=1)
    
    # Dot product of normalized vectors gives cosine similarity
    sims = torch.sum(tensor_embeddings[:-1] * tensor_embeddings[1:], dim=1)
    
    return sims.tolist()

def smooth_similarities(similarities: list[float], window_size: int = 1) -> list[float]:
    """
    Smooth similarities using a simple moving average.
    """
    if not similarities:
        return []
    
    smoothed = []
    n = len(similarities)
    for i in range(n):
        start = max(0, i - window_size)
        end = min(n, i + window_size + 1)
        window = similarities[start:end]
        smoothed.append(sum(window) / len(window))
    return smoothed

def find_valleys(similarities: list[float], k: float = 1.0) -> list[int]:
    """
    Find local minima (valleys) in similarities that are deeper than mean - k * std.
    """
    if not similarities or len(similarities) < 3:
        return []
        
    tensor_sims = torch.tensor(similarities, dtype=torch.float32)
    mean = tensor_sims.mean().item()
    std = tensor_sims.std(unbiased=False).item()
    threshold = mean - k * std
    
    valleys = []
    for i in range(1, len(similarities) - 1):
        if similarities[i] < similarities[i-1] and similarities[i] < similarities[i+1]:
            if similarities[i] < threshold:
                valleys.append(i)
                
    return valleys

def calculate_prefix_sums(embeddings: list[list[float]]) -> torch.Tensor:
    """
    Calculate prefix sums of normalized embeddings for O(1) coherence scoring.
    """
    if not embeddings:
        return torch.tensor([])
        
    tensor_embeddings = torch.tensor(embeddings, dtype=torch.float32)
    tensor_embeddings = F.normalize(tensor_embeddings, p=2, dim=1)
    
    cumsum = torch.cumsum(tensor_embeddings, dim=0)
    zeros = torch.zeros(1, cumsum.size(1))
    return torch.cat((zeros, cumsum), dim=0)

def calculate_information_density(text: str) -> float:
    """
    Heuristic to estimate information density using numbers, capitalized words, and symbols.
    """
    numbers = len(re.findall(r'\b\d+(?:\.\d+)?\b', text))
    caps = len(re.findall(r'\b[A-Z][A-Za-z0-9]+\b', text))
    symbols = len(re.findall(r'[$%]', text))
    return float(numbers + caps + symbols)

def segment_dp(
    embeddings: list[list[float]],
    valleys: list[int],
    densities: list[float],
    max_len: int = 50,
    penalty_weight: float = 0.1,
    penalty_exponent: float = 1.0,
    density_weight: float = 0.5
) -> list[tuple[int, int]]:
    """
    Dynamic programming sentence segmentation over candidate valley boundaries.
    """
    N = len(embeddings)
    if N == 0:
        return []
    
    candidates = [0] + [v for v in valleys if 0 < v < N] + [N]
    
    final_candidates = [0]
    for c in candidates[1:]:
        while c - final_candidates[-1] > max_len:
            final_candidates.append(final_candidates[-1] + max_len)
        if final_candidates[-1] != c:
            final_candidates.append(c)
    
    candidates = final_candidates
    
    prefix_sums = calculate_prefix_sums(embeddings)
    density_tensor = torch.tensor(densities, dtype=torch.float32)
    density_cumsum = torch.cat((torch.zeros(1), torch.cumsum(density_tensor, dim=0)))
    
    dp = {0: 0.0}
    backtrack = {0: 0}
    
    for i in range(1, len(candidates)):
        curr_bnd = candidates[i]
        best_score = float('-inf')
        best_prev = 0
        
        for j in range(i-1, -1, -1):
            prev_bnd = candidates[j]
            length = curr_bnd - prev_bnd
            
            if length > max_len:
                break
                
            segment_sum = prefix_sums[curr_bnd] - prefix_sums[prev_bnd]
            coherence = torch.norm(segment_sum, p=2).item()
            
            info_sum = density_cumsum[curr_bnd] - density_cumsum[prev_bnd]
            penalty = penalty_weight * (length ** penalty_exponent)
            
            score = coherence + (density_weight * info_sum.item()) - penalty
            
            total_score = dp[prev_bnd] + score
            if total_score > best_score:
                best_score = total_score
                best_prev = prev_bnd
                
        dp[curr_bnd] = best_score
        backtrack[curr_bnd] = best_prev
        
    segments = []
    curr = N
    while curr > 0:
        prev = backtrack[curr]
        segments.append((prev, curr))
        curr = prev
        
    segments.reverse()
    return segments

class SegmentationBackbone:
    def __init__(self, max_len: int = 50, penalty_weight: float = 0.1, density_weight: float = 0.5):
        self.max_len = max_len
        self.penalty_weight = penalty_weight
        self.density_weight = density_weight

    def segment_document(self, sentences: list[dict], context) -> list[tuple[int, int]]:
        """
        sentences: list of dicts with 'text', 'start_char', 'end_char'
        """
        if not sentences:
            return []
            
        embeddings = [context.pool_span(s['start_char'], s['end_char']) for s in sentences]
        densities = [calculate_information_density(s['text']) for s in sentences]
        
        sims = calculate_adjacent_similarities(embeddings)
        smoothed = smooth_similarities(sims, window_size=1)
        valleys = find_valleys(smoothed, k=1.0)
        
        segment_indices = segment_dp(
            embeddings=embeddings,
            valleys=valleys,
            densities=densities,
            max_len=self.max_len,
            penalty_weight=self.penalty_weight,
            density_weight=self.density_weight
        )
        
        char_spans = []
        for start_idx, end_idx in segment_indices:
            start_char = sentences[start_idx]['start_char']
            end_char = sentences[end_idx - 1]['end_char']
            char_spans.append((start_char, end_char))
            
        return char_spans
