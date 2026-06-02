import re

from iknos.core.embeddings import EmbeddingSubstrate
from iknos.core.segmentation import SegmentationBackbone

def get_sentences(text):
    sentences = []
    # Basic heuristic sentence splitter that preserves offsets
    # Looks for sentence-ending punctuation followed by space or newline
    pattern = re.compile(r'[^.!?\n]+(?:[.!?]+(?=\s|$)|$)')
    for match in pattern.finditer(text):
        s_text = match.group().strip()
        if not s_text:
            continue
        sentences.append({
            'text': s_text,
            'start_char': match.start(),
            'end_char': match.end()
        })
    return sentences

def main():
    file_path = "input/samples/attention.md"
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    print(f"Loaded {file_path} ({len(text)} characters)")
    
    sentences = get_sentences(text)
    print(f"Parsed {len(sentences)} sentences.")
    
    print("Loading EmbeddingSubstrate (bge-m3)...")
    substrate = EmbeddingSubstrate()
    
    print("Embedding document (late chunking)...")
    context = substrate.embed_document(text)
    
    print("Running SegmentationBackbone...")
    # Tweak max_len depending on expected segment size
    backbone = SegmentationBackbone(max_len=10, penalty_weight=0.1, density_weight=0.5)
    char_spans = backbone.segment_document(sentences, context)
    
    print(f"\n=== Found {len(char_spans)} optimal segments ===\n")
    
    for i, (start_char, end_char) in enumerate(char_spans):
        chunk_text = text[start_char:end_char].strip()
        preview = chunk_text[:120].replace("\n", " ") + ("..." if len(chunk_text) > 120 else "")
        print(f"Segment {i+1} [Chars {start_char}-{end_char}] ({len(chunk_text)} chars)")
        print(f"Preview: {preview}\n")

if __name__ == "__main__":
    main()
