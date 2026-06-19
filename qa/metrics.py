"""Text-comparison metrics for the QA toolkit (no external dependencies).

CER (character error rate) and WER (word error rate) measure how far the bot's
OCR output is from a known-good reference, using Levenshtein edit distance:

    rate = edit_distance(reference, hypothesis) / len(reference)

0.0 means a perfect match; values grow with errors and can exceed 1.0 when the
hypothesis has many insertions. We report a *normalized* variant (lowercased,
whitespace-collapsed) so cosmetic differences don't dominate the score.
"""
import re
import unicodedata


def _levenshtein(ref, hyp):
    """Edit distance between two sequences (lists of chars or words).

    O(len(ref) * len(hyp)) time, O(len(ref)) memory — fine for document-sized
    OCR output. Works on any sequence of comparable, hashable items.
    """
    if ref == hyp:
        return 0
    if not ref:
        return len(hyp)
    if not hyp:
        return len(ref)
    prev = list(range(len(ref) + 1))
    for j, h in enumerate(hyp, start=1):
        curr = [j]
        for i, r in enumerate(ref, start=1):
            cost = 0 if r == h else 1
            curr.append(min(
                prev[i] + 1,        # deletion
                curr[i - 1] + 1,    # insertion
                prev[i - 1] + cost  # substitution
            ))
        prev = curr
    return prev[-1]


def normalize_for_compare(text):
    """Canonicalize text so cosmetic differences don't inflate the error rate:
    Unicode NFC, lowercased, all whitespace runs collapsed to single spaces."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def cer(reference, hypothesis, normalize=True):
    """Character error rate. Returns None if the reference is empty."""
    ref, hyp = reference or "", hypothesis or ""
    if normalize:
        ref, hyp = normalize_for_compare(ref), normalize_for_compare(hyp)
    if not ref:
        return None
    return _levenshtein(list(ref), list(hyp)) / len(ref)


def wer(reference, hypothesis, normalize=True):
    """Word error rate. Returns None if the reference has no words."""
    ref, hyp = reference or "", hypothesis or ""
    if normalize:
        ref, hyp = normalize_for_compare(ref), normalize_for_compare(hyp)
    ref_words, hyp_words = ref.split(), hyp.split()
    if not ref_words:
        return None
    return _levenshtein(ref_words, hyp_words) / len(ref_words)
