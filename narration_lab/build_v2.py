#!/usr/bin/env python3
"""Narration v2: sentence-level FrankenTTS reads, auditioned and scored, then
edited and assembled into one WAV per script line the way a voice-over
editor would cut a session.

Why: a whole multi-sentence line spoken in one TTS call drifts in pace and
intonation, and hard names get mangled. So:

- every sentence is synthesized on its own, giving each its own natural
  sentence-final contour;
- names are respelled for the TTS input only, and every respelling in
  SPELLINGS is auditioned (FrankenTTS 0.1.11 decodes greedily, so identical
  text gives identical audio; different reads need different text);
- `alts.tsv` holds director-written alternate phrasings for sentences whose
  reads come out badly;
- each read is scored on intelligibility (ASR match and word confidence),
  articulation rate in syllables per second, unprompted hesitations, pitch
  movement, and the sentence-final contour (statements fall, yes/no
  questions rise);
- the winning read is edited: hesitations that the script doesn't license are
  tightened, comma pauses are capped at a natural phrase break, a rushed read
  is slowed gently (at most 12%, formant-preserving rubberband), and the level
  is matched;
- sentences are joined with gaps chosen by meaning: a beat after a question
  and before a turn ("But", "So", "Now"), a quick join into a short payoff.

Read tags are content-addressed (`<id>__s<j>__<hash>`), so re-running after a
script or alternates edit only synthesizes and transcribes the new reads.

    python3 build_v2.py plan   SCRIPT.tsv WORK_DIR [--alts alts.tsv]   # -> takes.tsv
    (python3 tts_batch.py WORK_DIR/takes.tsv WORK_DIR/takes)
    (uv run ... asr_batch.py WORK_DIR/takes WORK_DIR/asr.json)
    python3 build_v2.py choose SCRIPT.tsv WORK_DIR OUT_DIR                # -> OUT_DIR/<id>.wav + review
"""
import argparse
import difflib
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

# Respellings auditioned for the TTS input (the first is the default).
# Hoeffding: the owner wants the anglicized HEFF-ding.
SPELLINGS = {
    "Hoeffding": ["Heffding", "Hefding", "Heff-ding"],
    "Wassily": ["Vassily"],
    "Spearman": ["Speerman"],
    "Pearson": ["Peerson", "Pearson", "Peer-son"],
    "manim": ["manim", "mannim", "man-im"],
}
# ASR spellings accepted for a canonical word: lowercase, apostrophes removed.
# A /d/ after /f/ partly devoices and the ASR's language model prefers the
# real word "hefting", so that spelling counts; "hifting" (wrong vowel) and
# "person" (wrong name) do not.
ACCEPT = {
    "hoeffding": {"hefting", "hefding", "heffding", "heffting"},
    "wassily": {"vasily", "vassily", "vasilly", "vasili", "vassili"},
    "spearman": {"speerman", "spearmen"},
    "pearson": {"pierson", "peerson", "pearsons"},
    "manim": {"manum", "mannim", "manem", "mannum"},
    "tau": {"tao", "tow"},
    "q": {"cue", "queue"},
    "franken": {"frankin"},
    "by": {"x"},
}
ALIAS = {heard: canon for canon, forms in ACCEPT.items() for heard in forms}
PHRASES = [("frank and", "franken"), ("frank in", "franken"), ("a hundred", "one hundred"),
           ("a thousand", "one thousand"), ("4x4", "four by four"), ("-0.5", "minus one half")]
ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
YES_NO = ("are", "is", "could", "can", "do", "does", "did", "will", "would", "should", "has", "have")
TURN_WORDS = ("but ", "so ", "now ", "and finally", "then ", "once again", "in the end", "keep in mind",
              "look at", "next,", "the trade-off")

SYL_TARGET, SYL_SIGMA = 4.4, 0.6   # articulation rate (syllables/s, pauses excluded): a calm, measured read
SYL_MAX = 4.75                      # faster than this gets slowed
STRETCH_FLOOR = 0.88                # never slow by more than 12%
COMMA_PAUSE_CAP = 0.36              # a phrase break at a comma
OTHER_PAUSE_CAP = 0.14              # a hesitation the script doesn't license
TARGET_RMS_DB = -20.0
RATE = 24000


# ------------------------------------------------------------------ text

def say_int(n):
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " and " + say_int(n % 100))
    if 1100 <= n <= 1999:  # years
        return say_int(n // 100) + " " + (say_int(n % 100) if n % 100 >= 10 else "oh " + ONES[n % 100])
    if n < 1_000_000:
        rest = n % 1000
        return say_int(n // 1000) + " thousand" + ("" if rest == 0 else (" and " if rest < 100 else " ") + say_int(rest))
    return " ".join(ONES[int(c)] for c in str(n))


def say_number(tok):
    neg, tok = tok.startswith("-"), tok.lstrip("-").replace(",", "")
    if "." in tok:
        whole, frac = tok.split(".", 1)
        spoken = say_int(int(whole or 0)) + " point " + " ".join(ONES[int(c)] for c in frac)
    else:
        spoken = say_int(int(tok))
    return ("minus " if neg else "") + spoken


def canon(word):
    if word in ALIAS:
        return ALIAS[word]
    if word.endswith("s") and word[:-1] in ALIAS:
        return ALIAS[word[:-1]] + "s"
    return word


def tokens(text):
    """Normalized word tokens, each with the punctuation that follows it in
    the source text ("," for a phrase break, "." for a sentence end)."""
    text = text.lower().replace("—", ", ").replace("–", " ").replace("’", "'")
    for a, b in PHRASES:
        text = text.replace(a, b)
    out = []
    for raw in text.split():
        punct = "," if raw.endswith((",", ";", ":")) else ("." if raw.endswith((".", "?", "!")) else "")
        parts = []
        for t in re.findall(r"-?\d[\d,.]*\d|-?\d|[a-z]+", raw.replace("'", "")):
            if t[0].isdigit() or t[0] == "-":
                parts.extend(say_number(t.rstrip(".,")).replace("-", " ").split())
            else:
                parts.append(canon(t))
        for k, p in enumerate(parts):
            out.append((p, punct if k == len(parts) - 1 else ""))
    return out


def syllables(word):
    if len(word) == 1:
        return 3 if word == "w" else 1
    n = len(re.findall(r"[aeiouy]+", word))
    if n > 1 and word.endswith("e") and not word.endswith(("le", "ee", "ye")):
        n -= 1
    elif n > 1 and word.endswith(("es", "ed")) and not word.endswith(
            ("ted", "ded", "ses", "ces", "zes", "ges", "xes", "shes", "ches")):
        n -= 1
    return max(1, n)


def tts_variants(text):
    """Every respelling to audition for one sentence, as TTS input strings."""
    present = [w for w in SPELLINGS if re.search(rf"\b{w}\b", text)]
    count = max((len(SPELLINGS[w]) for w in present), default=1)
    out = []
    for k in range(count):
        spoken = text
        for w in present:
            spoken = re.sub(rf"\b{w}\b", SPELLINGS[w][min(k, len(SPELLINGS[w]) - 1)], spoken)
        if spoken not in out:
            out.append(spoken)
    return out


def sentences(text):
    return [p for p in re.split(r"(?<=[.?!])\s+", text.strip()) if p]


def read_script(path):
    return [l.rstrip("\n").split("\t", 1) for l in open(path) if "\t" in l]


def gap_between(prev, nxt):
    """Silence between two sentences of one line, chosen by meaning."""
    if prev.rstrip().endswith("?"):
        return 0.52
    if nxt.lower().startswith(TURN_WORDS):
        return 0.46
    if len(nxt.split()) <= 4 or len(prev.split()) <= 3:  # quick payoff ("Same story.") / lead-in
        return 0.30
    return 0.38


def align(want, heard):
    """Match ratio between script and transcript, forgiving compound spellings
    ("scatter plot" vs "scatterplot", "t t s" vs "tts") but not mishearings,
    plus a map from heard index to script index."""
    sm = difflib.SequenceMatcher(None, want, heard, autojunk=False)
    matched, heard_len, to_want = 0, len(heard), {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            matched += i2 - i1
            to_want.update({j1 + d: i1 + d for d in range(j2 - j1)})
        elif tag == "replace" and "".join(want[i1:i2]) == "".join(heard[j1:j2]):
            matched += i2 - i1
            heard_len += (i2 - i1) - (j2 - j1)
            to_want.update({j: i2 - 1 for j in range(j1, j2)})
        elif i2 > i1:
            to_want.update({j: min(i2 - 1, i1 + (j - j1)) for j in range(j1, j2)})
    total = len(want) + heard_len
    return (2 * matched / total if total else 1.0), to_want


# ------------------------------------------------------------------ audio

def load(path):
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64) / 32768.0
        if w.getnchannels() == 2:
            pcm = pcm.reshape(-1, 2).mean(axis=1)
    return pcm, rate


def write_wav(path, x, rate=RATE):
    pcm = (np.clip(x, -0.98, 0.98) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def frame_db(x, rate, win=0.02, hop=0.01):
    n, h = int(win * rate), int(hop * rate)
    if len(x) < n:
        return np.array([-120.0])
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::h]
    return 20 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12))


def f0_track(x, rate, hop=0.01, win=0.04, fmin=65.0, fmax=320.0):
    """Normalized-autocorrelation pitch per frame (Hz, nan when unvoiced)."""
    n, h = int(win * rate), int(hop * rate)
    lag_min, lag_max = int(rate / fmax), int(rate / fmin)
    out = []
    for start in range(0, len(x) - n, h):
        frame = x[start:start + n] * np.hanning(n)
        if np.sqrt(np.mean(frame ** 2)) < 10 ** (-40 / 20):
            out.append(np.nan)
            continue
        spec = np.fft.rfft(frame, 2 * n)
        ac = np.fft.irfft(spec * np.conj(spec))[:n]
        ac /= ac[0] + 1e-12
        seg = ac[lag_min:lag_max]
        out.append(rate / (int(np.argmax(seg)) + lag_min) if seg.max() > 0.45 else np.nan)
    return np.array(out)


def silent_runs(db, thresh, first, last, min_len=0.10):
    """Internal silences (seconds, relative to the file) between first and last voiced frames."""
    runs, start = [], None
    for i in range(first, last + 1):
        if db[i] <= thresh:
            start = i if start is None else start
        elif start is not None:
            if (i - start) * 0.01 >= min_len:
                runs.append((start * 0.01, i * 0.01))
            start = None
    return runs


def analyze(path, asr, target_text):
    x, rate = load(path)
    db = frame_db(x, rate)
    thresh = max(-48.0, db.max() - 38.0)
    voiced = np.where(db > thresh)[0]
    if len(voiced) == 0:
        return None
    first, last = voiced[0], voiced[-1]
    start_s, end_s = first * 0.01, last * 0.01 + 0.02
    want_tok = tokens(target_text)
    want = [w for w, _ in want_tok]
    heard_tok = []
    for w in asr.get("words", []):
        for t, _ in tokens(w["w"]):
            heard_tok.append((t, w["s"], w["e"], w["p"]))
    ratio, to_want = align(want, [t for t, *_ in heard_tok])
    # Classify each internal silence by the script punctuation before it.
    pauses = []
    for a, b in silent_runs(db, thresh, first, last):
        before = [j for j, (_, s, e, _) in enumerate(heard_tok) if e <= a + 0.25]
        punct = want_tok[to_want[before[-1]]][1] if before and before[-1] in to_want else "?"
        pauses.append({"at": round(a, 2), "len": round(b - a, 2), "punct": punct})
    hesitations = [p for p in pauses if p["punct"] == "" and p["len"] >= 0.22]
    articulation = (end_s - start_s) - sum(p["len"] for p in pauses)
    syl = sum(syllables(w) for w in want)
    syl_rate = syl / articulation if articulation > 0 else 0.0
    probs = [p for *_, p in heard_tok] or [0.0]
    f0 = f0_track(x[int(start_s * rate):int(end_s * rate)], rate)
    voiced_f0 = f0[~np.isnan(f0)]
    pitch_std, final_st = 0.0, 0.0
    if len(voiced_f0) > 10:
        st = 12 * np.log2(voiced_f0 / np.median(voiced_f0))
        st = st[np.abs(st) < 12]
        if len(st) > 8:
            pitch_std = float(np.std(st))
            final_st = float(np.median(st[-6:]))
    question = target_text.rstrip().endswith("?")
    yes_no = question and target_text.split()[0].lower() in YES_NO
    if yes_no:
        s_contour = float(np.clip((final_st + 0.5) / 2.5, 0, 1))      # wants a rise
    elif question:
        s_contour = 1.0                                               # wh-question: either is fine
    else:
        s_contour = float(np.clip((2.5 - final_st) / 2.5, 0, 1))      # statements fall; uptalk loses
    s_conf = float(np.clip((np.mean(probs) - 0.5) / 0.45, 0, 1))
    s_rate = math.exp(-((min(syl_rate, SYL_MAX + 0.6) - SYL_TARGET) / SYL_SIGMA) ** 2 / 2)
    s_pause = 1.0 / (1.0 + sum(p["len"] for p in hesitations) * 3)
    s_pitch = pitch_std / 1.2 if pitch_std < 1.2 else (max(0.0, 1 - (pitch_std - 6.5) / 4) if pitch_std > 6.5 else 1.0)
    score = 0.40 * ratio + 0.15 * s_conf + 0.15 * s_rate + 0.10 * s_pause + 0.10 * s_pitch + 0.10 * s_contour
    if ratio < 0.85:
        score -= 0.5
    return {
        "score": round(score, 4), "asr_ratio": round(ratio, 3), "conf": round(float(np.mean(probs)), 3),
        "syl_rate": round(syl_rate, 2), "pauses": pauses, "hesitations": len(hesitations),
        "pitch_std_st": round(pitch_std, 2), "final_st": round(final_st, 2),
        "start": round(start_s, 3), "end": round(end_s, 3), "heard": asr.get("text", ""),
    }


def crossfade_join(parts, rate=RATE, fade=0.01):
    n = int(fade * rate)
    out = parts[0]
    for p in parts[1:]:
        if len(out) > n and len(p) > n:
            mix = out[-n:] * np.linspace(1, 0, n) + p[:n] * np.linspace(0, 1, n)
            out = np.concatenate([out[:-n], mix, p[n:]])
        else:
            out = np.concatenate([out, p])
    return out


def edit(path, m):
    """Cut the winning read: tighten pauses, slow a rushed read, match level."""
    x, rate = load(path)
    assert rate == RATE, f"{path}: {rate} Hz"
    lead = max(0.0, m["start"] - 0.05)
    tail = min(len(x) / rate, m["end"] + 0.12)
    cuts, removed = [], 0.0
    for p in m["pauses"]:
        cap = COMMA_PAUSE_CAP if p["punct"] in (",", ".") else (0.30 if p["punct"] == "?" else OTHER_PAUSE_CAP)
        if p["len"] > cap:
            mid = p["at"] + p["len"] / 2
            half = (p["len"] - cap) / 2
            cuts.append((mid - half, mid + half))
            removed += p["len"] - cap
    parts, pos = [], lead
    for a, b in cuts:
        parts.append(x[int(pos * rate):int(a * rate)])
        pos = b
    parts.append(x[int(pos * rate):int(tail * rate)])
    seg = crossfade_join(parts)
    tempo = 1.0
    if m["syl_rate"] > SYL_MAX:
        tempo = max(STRETCH_FLOOR, (SYL_MAX - 0.2) / m["syl_rate"])
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.wav", Path(tmp) / "b.wav"
            write_wav(a, seg)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(a), "-af",
                            f"rubberband=tempo={tempo:.4f}:formant=preserved:pitchq=quality", str(b)], check=True)
            seg, _ = load(b)
    db = frame_db(seg, rate)
    loud = db[db > db.max() - 25]
    level = float(np.mean(loud)) if len(loud) else -30.0
    seg = seg * 10 ** ((TARGET_RMS_DB - level) / 20)
    fade = int(0.03 * rate)
    seg[:fade] *= np.linspace(0, 1, fade)
    seg[-fade:] *= np.linspace(1, 0, fade)
    return seg, tempo, round(removed, 2)


# --------------------------------------------------------------- commands

def tag_for(line_id, j, tts):
    return f"{line_id}__s{j}__{hashlib.sha1(tts.encode()).hexdigest()[:8]}"


def cmd_plan(args):
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    alts = {}
    if args.alts and Path(args.alts).exists():
        for line in open(args.alts):
            if line.count("\t") >= 2 and not line.startswith("#"):
                lid, j, text = line.rstrip("\n").split("\t", 2)
                alts.setdefault((lid, int(j)), []).append(text)
    rows, plan = [], {}
    for line_id, text in read_script(args.script):
        plan[line_id] = []
        for j, sent in enumerate(sentences(text)):
            reads = []
            for variant in [sent] + alts.get((line_id, j), []):
                for tts in tts_variants(variant):
                    tag = tag_for(line_id, j, tts)
                    reads.append({"tag": tag, "text": variant, "tts": tts})
                    rows.append(f"{tag}\t{tts}")
            plan[line_id].append({"j": j, "text": sent, "reads": reads})
    (work / "takes.tsv").write_text("\n".join(rows) + "\n")
    (work / "plan.json").write_text(json.dumps(plan, indent=1))
    print(f"{len(plan)} lines, {sum(len(v) for v in plan.values())} sentences, {len(rows)} reads -> {work / 'takes.tsv'}")


def cmd_choose(args):
    work, out = Path(args.work), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    plan = json.loads((work / "plan.json").read_text())
    asr = json.loads((work / "asr.json").read_text())
    report, weak = {}, []
    review = ["line\tsent\tscore\tasr\tsyl/s\ttempo\tcut_s\thesit\tpitch_sd\tfinal_st\ttts\theard"]
    for line_id, sents in plan.items():
        pieces, chosen = [], []
        for s in sents:
            cands = []
            for r in s["reads"]:
                wav = work / "takes" / f"{r['tag']}.wav"
                if not wav.exists() or r["tag"] not in asr:
                    continue
                m = analyze(wav, asr[r["tag"]], r["text"])
                if m:
                    cands.append((m["score"], wav, m, r))
            if not cands:
                sys.exit(f"no usable read for {line_id} sentence {s['j']}")
            cands.sort(key=lambda c: -c[0])
            _, best_wav, best, r = cands[0]
            seg, tempo, removed = edit(best_wav, best)
            chosen.append({"sentence": r["text"], "tts": r["tts"], "read": r["tag"], "tempo": round(tempo, 3),
                           "pause_cut_s": removed, **best,
                           "others": [{"tts": rr["tts"], "score": m["score"], "heard": m["heard"]}
                                      for _, _, m, rr in cands[1:]]})
            review.append(f"{line_id}\t{s['j']}\t{best['score']}\t{best['asr_ratio']}\t{best['syl_rate']}\t{tempo:.3f}\t"
                          f"{removed}\t{best['hesitations']}\t{best['pitch_std_st']}\t{best['final_st']}\t"
                          f"{r['tts']}\t{best['heard']}")
            if best["asr_ratio"] < 0.9 or best["hesitations"] or best["syl_rate"] > SYL_MAX / STRETCH_FLOOR \
                    or best["syl_rate"] < 3.4 or best["pitch_std_st"] < 1.2 or best["score"] < 0.8:
                weak.append(f"{r['tag']}: score {best['score']} ratio {best['asr_ratio']} syl/s {best['syl_rate']} "
                            f"hesitations {best['hesitations']} final {best['final_st']} | {r['tts']!r} -> {best['heard']!r}")
            pieces.append((seg, r["text"]))
        audio = []
        for i, (seg, text) in enumerate(pieces):
            audio.append(seg)
            if i + 1 < len(pieces):
                audio.append(np.zeros(int(gap_between(text, pieces[i + 1][1]) * RATE)))
        write_wav(out / f"{line_id}.wav", np.concatenate(audio))
        report[line_id] = chosen
    (out / "report.json").write_text(json.dumps(report, indent=1))
    (out / "review.tsv").write_text("\n".join(review) + "\n")
    reads = [c for line in report.values() for c in line]
    stretched = [c["tempo"] for c in reads if c["tempo"] < 1.0]
    print(f"assembled {len(report)} lines from {len(reads)} sentences -> {out}")
    print(f"median articulation {np.median([c['syl_rate'] for c in reads]):.2f} syl/s; "
          f"{len(stretched)} reads slowed (median tempo {np.median(stretched) if stretched else 1:.3f}); "
          f"{sum(c['pause_cut_s'] for c in reads):.1f} s of pauses tightened")
    print(f"{len(weak)} sentences flagged:")
    for w in weak:
        print("  ", w)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("script")
    p.add_argument("work")
    p.add_argument("--alts", default="alts.tsv")
    c = sub.add_parser("choose")
    c.add_argument("script")
    c.add_argument("work")
    c.add_argument("out")
    args = ap.parse_args()
    {"plan": cmd_plan, "choose": cmd_choose}[args.cmd](args)


if __name__ == "__main__":
    main()
