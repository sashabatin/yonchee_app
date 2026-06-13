"""Generate a synthetic multilingual QA corpus (deterministic, no network).

Renders embedded text snippets in many languages to images — both clean and
"degraded" (noise / blur / rotation / contrast / downscale / JPEG artifacts, to
approximate a phone photo or scan) — plus a few PDFs and mixed-language cases.
Because we author the text, every generated file ships with an exact ground
truth, so CER/WER is computed for the whole corpus (not just a couple of cases).

What this is good for: broad language-detection coverage and an OCR-robustness
stress layer. What it is NOT: a substitute for real photos of physical pages
(perspective, glare, paper curvature) — drop those into corpus/images/ when you
have them. Clean rendered text is read near-perfectly by Azure; the degraded
variants are where the signal is.

Output (all git-ignored, reproducible from this script):
    corpus/gen/images/<id>.png|pdf
    corpus/gen/expected/<id>.txt
    cases.gen.json            # merged with cases.json by run_ocr.py

Usage:
    python qa/generate_corpus.py            # ~100 cases, seed 42
    python qa/generate_corpus.py --seed 7
    python qa/generate_corpus.py --per-lang-clean 2 --per-lang-degraded 2
"""
import argparse
import io
import json
import random
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

QA_DIR = Path(__file__).resolve().parent
GEN_DIR = QA_DIR / "corpus" / "gen"
IMG_DIR = GEN_DIR / "images"
EXP_DIR = GEN_DIR / "expected"
MANIFEST = QA_DIR / "cases.gen.json"

FONTS = "C:/Windows/Fonts"
# Per-language fonts. Latin/Cyrillic/Greek are covered by Arial + Segoe UI;
# CJK/Korean/Thai need script-specific fonts. Languages whose script has no word
# spaces (wrap by character) are flagged in NO_SPACE.
LATIN_FONTS = [f"{FONTS}/arial.ttf", f"{FONTS}/segoeui.ttf"]
FONT_FOR = {
    "zh": [f"{FONTS}/msyh.ttc"],
    "ja": [f"{FONTS}/YuGothR.ttc", f"{FONTS}/msgothic.ttc"],
    "ko": [f"{FONTS}/malgun.ttf"],
    "th": [f"{FONTS}/leelawui.ttf"],
}
NO_SPACE = {"zh", "ja", "th"}

# Three sentences per language (so we can vary length: short=1, medium=2, full=3).
# Same neutral content everywhere, to keep cross-language results comparable.
TEXTS = {
    "en": ["The city library opened a new reading room this spring.",
           "Visitors can borrow books in more than twenty languages and join free evening lectures.",
           "Staff say the number of young readers has grown steadily over the past year."],
    "es": ["La biblioteca municipal inauguró una nueva sala de lectura esta primavera.",
           "Los visitantes pueden tomar prestados libros en más de veinte idiomas y asistir a conferencias gratuitas por la tarde.",
           "El personal afirma que el número de lectores jóvenes ha crecido de forma constante durante el último año."],
    "de": ["Die Stadtbibliothek hat in diesem Frühjahr einen neuen Lesesaal eröffnet.",
           "Besucher können Bücher in mehr als zwanzig Sprachen ausleihen und an kostenlosen Abendvorträgen teilnehmen.",
           "Die Mitarbeiter berichten, dass die Zahl der jungen Leser im vergangenen Jahr stetig gestiegen ist."],
    "fr": ["La bibliothèque municipale a inauguré une nouvelle salle de lecture ce printemps.",
           "Les visiteurs peuvent emprunter des livres dans plus de vingt langues et assister à des conférences gratuites en soirée.",
           "Le personnel indique que le nombre de jeunes lecteurs a augmenté régulièrement au cours de l'année écoulée."],
    "pl": ["Biblioteka miejska otworzyła tej wiosny nową czytelnię.",
           "Odwiedzający mogą wypożyczać książki w ponad dwudziestu językach i uczestniczyć w bezpłatnych wieczornych wykładach.",
           "Pracownicy twierdzą, że liczba młodych czytelników stale rosła w ciągu ostatniego roku."],
    "pt": ["A biblioteca municipal inaugurou uma nova sala de leitura nesta primavera.",
           "Os visitantes podem pegar emprestados livros em mais de vinte idiomas e participar de palestras gratuitas à noite.",
           "Os funcionários afirmam que o número de jovens leitores cresceu de forma constante no último ano."],
    "it": ["La biblioteca comunale ha inaugurato una nuova sala di lettura questa primavera.",
           "I visitatori possono prendere in prestito libri in più di venti lingue e partecipare a conferenze serali gratuite.",
           "Il personale afferma che il numero di giovani lettori è cresciuto costantemente nell'ultimo anno."],
    "nl": ["De stadsbibliotheek heeft dit voorjaar een nieuwe leeszaal geopend.",
           "Bezoekers kunnen boeken lenen in meer dan twintig talen en gratis avondlezingen bijwonen.",
           "Medewerkers zeggen dat het aantal jonge lezers het afgelopen jaar gestaag is gegroeid."],
    "tr": ["Şehir kütüphanesi bu ilkbaharda yeni bir okuma salonu açtı.",
           "Ziyaretçiler yirmiden fazla dilde kitap ödünç alabilir ve ücretsiz akşam konferanslarına katılabilir.",
           "Çalışanlar, genç okuyucu sayısının geçen yıl boyunca istikrarlı bir şekilde arttığını söylüyor."],
    "ru": ["Городская библиотека открыла новый читальный зал этой весной.",
           "Посетители могут брать книги более чем на двадцати языках и посещать бесплатные вечерние лекции.",
           "Сотрудники отмечают, что число молодых читателей за прошедший год неуклонно росло."],
    "uk": ["Міська бібліотека відкрила цієї весни нову читальну залу.",
           "Відвідувачі можуть брати книжки більш ніж двадцятьма мовами та відвідувати безкоштовні вечірні лекції.",
           "Працівники зазначають, що кількість молодих читачів за минулий рік неухильно зростала."],
    "kk": ["Қалалық кітапхана осы көктемде жаңа оқу залын ашты.",
           "Келушілер жиырмадан астам тілде кітап ала алады және тегін кешкі дәрістерге қатыса алады.",
           "Қызметкерлердің айтуынша, өткен жылы жас оқырмандар саны тұрақты түрде өсті."],
    "el": ["Η δημοτική βιβλιοθήκη εγκαινίασε μια νέα αίθουσα ανάγνωσης αυτή την άνοιξη.",
           "Οι επισκέπτες μπορούν να δανειστούν βιβλία σε περισσότερες από είκοσι γλώσσες και να παρακολουθήσουν δωρεάν βραδινές διαλέξεις.",
           "Το προσωπικό αναφέρει ότι ο αριθμός των νέων αναγνωστών αυξανόταν σταθερά τον τελευταίο χρόνο."],
    "zh": ["市图书馆今年春天开放了一间新的阅览室。",
           "读者可以借阅二十多种语言的书籍，并参加免费的晚间讲座。",
           "工作人员表示，过去一年里年轻读者的数量稳步增长。"],
    "ja": ["市立図書館はこの春、新しい閲覧室を開設しました。",
           "来館者は二十以上の言語の本を借りることができ、無料の夜間講座にも参加できます。",
           "職員によると、若い読者の数はこの一年で着実に増えているそうです。"],
    "ko": ["시립 도서관이 올봄 새로운 열람실을 열었습니다.",
           "방문객은 스무 개가 넘는 언어의 책을 빌릴 수 있고 무료 저녁 강연에도 참여할 수 있습니다.",
           "직원들은 지난 한 해 동안 젊은 독자 수가 꾸준히 늘었다고 말합니다."],
    "th": ["ห้องสมุดประจำเมืองเปิดห้องอ่านหนังสือแห่งใหม่ในฤดูใบไม้ผลินี้",
           "ผู้มาเยือนสามารถยืมหนังสือได้มากกว่ายี่สิบภาษาและเข้าร่วมการบรรยายตอนเย็นได้ฟรี",
           "เจ้าหน้าที่กล่าวว่าจำนวนผู้อ่านรุ่นเยาว์เพิ่มขึ้นอย่างต่อเนื่องในปีที่ผ่านมา"],
}

# Mixed-language pairs: dominant language first; we expect the bot's coverage
# guard to flag these (no confident single language) rather than auto-synthesize.
MIXED_PAIRS = [("en", "ru"), ("es", "zh"), ("de", "ko"), ("fr", "ja")]


def font_paths(lang):
    return FONT_FOR.get(lang, LATIN_FONTS)


def join_sentences(lang, sentences):
    sep = "" if lang in NO_SPACE else " "
    return sep.join(sentences)


def wrap(draw, text, font, max_width, by_char):
    """Greedy word/char wrap to max_width pixels. Returns a list of lines."""
    units = list(text) if by_char else text.split(" ")
    glue = "" if by_char else " "
    lines, cur = [], ""
    for u in units:
        trial = u if not cur else cur + glue + u
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = u
    if cur:
        lines.append(cur)
    return lines


def render(text, font, lang, width=1200, pad=48, fg=(20, 20, 20), bg=(255, 255, 255)):
    """Render wrapped text to a white RGB image sized to fit the content."""
    scratch = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = wrap(scratch, text, font, width - 2 * pad, by_char=lang in NO_SPACE)
    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * 1.35)
    height = pad * 2 + line_h * len(lines)
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    y = pad
    for ln in lines:
        draw.text((pad, y), ln, font=font, fill=fg)
        y += line_h
    return img


BG = (245, 244, 240)


def _motion_blur(img, rnd):
    """Approximate motion blur by averaging several directionally-offset copies."""
    n = rnd.choice([6, 9, 13])
    dx, dy = rnd.choice([(1, 0), (1, 1), (2, 1), (1, -1)])
    acc = img.convert("RGB")
    for i in range(1, n):
        acc = Image.blend(acc, ImageChops.offset(img, dx * i, dy * i), 1.0 / (i + 1))
    return acc


def _warp(img, rnd, amount):
    """Perspective-ish warp via a QUAD transform — maps a jittered source quad
    onto the output rectangle, simulating a photo taken at an angle."""
    w, h = img.size
    jx, jy = w * amount, h * amount

    def j(v):
        return rnd.uniform(0, v)
    # QUAD source corners: NW, SW, SE, NE.
    quad = (j(jx), j(jy), j(jx), h - j(jy),
            w - j(jx), h - j(jy), w - j(jx), j(jy))
    return img.transform((w, h), Image.QUAD, quad, Image.BICUBIC, fillcolor=BG)


def degrade(img, rnd, intensity="mild"):
    """Apply random photo-like degradations. Returns (image, [tags]).

    'mild' ≈ a decent phone snapshot; 'harsh' stacks a geometric warp with heavy
    blur/noise/low-resolution/compression to probe where OCR finally breaks.
    """
    harsh = intensity == "harsh"
    tags = []
    if harsh:
        # Always warp + rotate, then 2–3 destroyers.
        ops = ["warp", "rotate"] + rnd.sample(
            ["motion", "blur", "noise", "downscale", "jpeg", "contrast"],
            k=rnd.randint(2, 3))
    else:
        ops = rnd.sample(["rotate", "blur", "noise", "contrast", "downscale", "jpeg"],
                         k=rnd.randint(2, 3))
    for op in ops:
        if op == "warp":
            img = _warp(img, rnd, rnd.uniform(0.06, 0.13))
        elif op == "rotate":
            img = img.rotate(rnd.uniform(-9, 9) if harsh else rnd.uniform(-4, 4),
                             expand=True, fillcolor=BG, resample=Image.BICUBIC)
        elif op == "motion":
            img = _motion_blur(img, rnd)
        elif op == "blur":
            img = img.filter(ImageFilter.GaussianBlur(
                rnd.uniform(2.0, 4.0) if harsh else rnd.uniform(0.6, 1.6)))
        elif op == "noise":
            sigma = rnd.uniform(38, 72) if harsh else rnd.uniform(18, 36)
            alpha = rnd.uniform(0.22, 0.42) if harsh else rnd.uniform(0.08, 0.18)
            noise = Image.effect_noise(img.size, sigma).convert("RGB")
            img = Image.blend(img, noise, alpha)
        elif op == "contrast":
            lo, hi = (0.5, 1.5) if harsh else (0.7, 1.3)
            img = ImageEnhance.Contrast(img).enhance(rnd.uniform(lo, hi))
            img = ImageEnhance.Brightness(img).enhance(rnd.uniform(0.6, 1.25) if harsh
                                                       else rnd.uniform(0.8, 1.15))
        elif op == "downscale":
            w, h = img.size
            s = rnd.uniform(0.2, 0.35) if harsh else rnd.uniform(0.45, 0.7)
            img = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.BICUBIC)
            img = img.resize((w, h), Image.BICUBIC)
        elif op == "jpeg":
            q = rnd.randint(6, 16) if harsh else rnd.randint(18, 40)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q)
            buf.seek(0)
            img = Image.open(buf).convert("RGB")
        tags.append(op)
    return img, tags


def write_case(cases, cid, img, text, expected_lang, notes, as_pdf=False):
    ext = "pdf" if as_pdf else "png"
    img_rel = f"gen/images/{cid}.{ext}"
    exp_rel = f"gen/expected/{cid}.txt"
    if as_pdf:
        img.save(IMG_DIR / f"{cid}.{ext}", "PDF", resolution=150)
    else:
        img.save(IMG_DIR / f"{cid}.{ext}")
    (EXP_DIR / f"{cid}.txt").write_text(text, encoding="utf-8")
    cases.append({
        "id": cid, "file": img_rel, "expected_lang": expected_lang,
        "expected_text_file": exp_rel, "pinned_lang": None, "notes": notes,
    })


def main():
    ap = argparse.ArgumentParser(description="Generate a synthetic QA corpus.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--per-lang-clean", type=int, default=3)
    ap.add_argument("--per-lang-degraded", type=int, default=3)
    ap.add_argument("--per-lang-harsh", type=int, default=2,
                    help="heavily-degraded variants per language (warp+blur+noise+...)")
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    EXP_DIR.mkdir(parents=True, exist_ok=True)
    # Clear any previous generation so removed cases don't linger.
    for d in (IMG_DIR, EXP_DIR):
        for f in d.iterdir():
            if f.is_file():
                f.unlink()

    cases = []
    font_cache = {}

    def get_font(path, size):
        key = (path, size)
        if key not in font_cache:
            font_cache[key] = ImageFont.truetype(path, size)
        return font_cache[key]

    for lang, sentences in TEXTS.items():
        paths = font_paths(lang)
        # Clean: vary length and font/size for breadth.
        for i in range(args.per_lang_clean):
            n = [1, 2, 3][i % 3]
            text = join_sentences(lang, sentences[:n])
            font = get_font(paths[i % len(paths)], rnd.choice([30, 34, 40]))
            img = render(text, font, lang)
            write_case(cases, f"gen-{lang}-clean-{i+1}", img, text, lang,
                       f"clean, {n} sentence(s)")
        # Degraded: always the full paragraph (more text = clearer error signal).
        full = join_sentences(lang, sentences)
        for i in range(args.per_lang_degraded):
            font = get_font(paths[i % len(paths)], rnd.choice([32, 36, 42]))
            img = render(full, font, lang)
            img, tags = degrade(img, rnd, "mild")
            write_case(cases, f"gen-{lang}-degraded-{i+1}", img, full, lang,
                       "degraded: " + "+".join(tags))
        # Harsh: stacked geometric + heavy degradation to find break points.
        for i in range(args.per_lang_harsh):
            font = get_font(paths[i % len(paths)], rnd.choice([34, 40, 46]))
            img = render(full, font, lang)
            img, tags = degrade(img, rnd, "harsh")
            write_case(cases, f"gen-{lang}-harsh-{i+1}", img, full, lang,
                       "harsh: " + "+".join(tags))

    # A few image-based PDFs to exercise the pdf branch.
    for lang in ["en", "ru", "fr", "zh"]:
        text = join_sentences(lang, TEXTS[lang])
        font = get_font(font_paths(lang)[0], 36)
        write_case(cases, f"gen-{lang}-pdf", render(text, font, lang), text, lang,
                   "clean PDF", as_pdf=True)

    # Mixed-language: expected_lang=None (scoring skipped); kept for the coverage
    # guard check. Ground truth is the concatenation, so CER still applies.
    for a, b in MIXED_PAIRS:
        text = join_sentences(a, TEXTS[a]) + " " + join_sentences(b, TEXTS[b])
        font = get_font(LATIN_FONTS[0], 34)
        # Latin font won't render CJK 'b' halves; use the 'b' font if non-latin.
        if b in FONT_FOR:
            # Render two stacked blocks with the right fonts instead.
            top = render(join_sentences(a, TEXTS[a]), get_font(font_paths(a)[0], 34), a)
            bottom = render(join_sentences(b, TEXTS[b]), get_font(font_paths(b)[0], 34), b)
            w = max(top.width, bottom.width)
            img = Image.new("RGB", (w, top.height + bottom.height), (255, 255, 255))
            img.paste(top, (0, 0))
            img.paste(bottom, (0, top.height))
        else:
            img = render(text, font, a)
        write_case(cases, f"gen-mixed-{a}-{b}", img, text, None, f"mixed {a}+{b}")

    MANIFEST.write_text(
        json.dumps({"_comment": "GENERATED by generate_corpus.py — do not edit by hand; git-ignored.",
                    "cases": cases}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"Generated {len(cases)} cases -> {MANIFEST.relative_to(QA_DIR.parent)}")
    print(f"Images: {IMG_DIR.relative_to(QA_DIR.parent)}  (git-ignored)")


if __name__ == "__main__":
    main()
