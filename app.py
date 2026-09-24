# -*- coding: utf-8 -*-
"""
مُقطّع فيديوهات يوتيوب إلى Shorts/Reels عمودية (9:16)
====================================================
"""

import streamlit as st
import os
import re
import json
import shutil
import tempfile
import subprocess
from pathlib import Path
from datetime import timedelta

import yt_dlp
from groq import Groq

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    from moviepy.editor import VideoFileClip
    MOVIEPY_AVAILABLE = True
except ImportError:
    MOVIEPY_AVAILABLE = False


st.set_page_config(
    page_title="مُقطّع الفيديو الذكي",
    page_icon="🎬",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    .stButton>button { height: 3em; font-size: 1.1em; border-radius: 10px; }
    div[data-testid="stTextInput"] input { direction: ltr; }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


with st.sidebar:
    st.header("⚙️ الإعدادات")

    groq_api_key = st.text_input(
        "Groq API Key (للتفريغ الصوتي - مجاني)",
        type="password",
        value=get_secret("GROQ_API_KEY"),
        help="احصل عليه من console.groq.com",
    )

    use_claude = st.checkbox(
        "استخدام Claude لاختيار أفضل المقاطع",
        value=bool(get_secret("ANTHROPIC_API_KEY")),
        help="إذا كان غير مفعّل سيتم استخدام Llama 3.3 عبر Groq مجاناً بدلاً منه",
    )

    anthropic_api_key = ""
    if use_claude:
        anthropic_api_key = st.text_input(
            "Anthropic API Key",
            type="password",
            value=get_secret("ANTHROPIC_API_KEY"),
        )

    st.divider()
    num_clips = st.slider("عدد المقاطع المطلوبة", 1, 8, 3)
    clip_len = st.slider("المدة التقريبية لكل مقطع (ثانية)", 20, 90, 45, step=5)
    burn_subs = st.checkbox("حرق الترجمة داخل الفيديو", value=True)
    max_video_minutes = st.slider("حد أقصى لطول الفيديو المصدر (دقيقة)", 5, 120, 40)


st.title("🎬 محوّل يوتيوب إلى Shorts/Reels")
st.caption("الصق رابط فيديو يوتيوب طويل، واحصل على مقاطع قصيرة عمودية (9:16) جاهزة للنشر مع ترجمة آلية.")

youtube_url = st.text_input(
    "🔗 رابط فيديو يوتيوب",
    placeholder="https://www.youtube.com/watch?v=XXXXXXXXXXX",
)

generate_clicked = st.button("🚀 توليد المقاطع", use_container_width=True, type="primary")


def format_srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    td = timedelta(seconds=seconds)
    total_ms = int(td.total_seconds() * 1000)
    hours, rem = divmod(total_ms, 3600 * 1000)
    minutes, rem = divmod(rem, 60 * 1000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def run_cmd(cmd: list, desc: str = ""):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"فشل تنفيذ: {desc}\n{result.stderr[-2000:]}")
    return result


def download_video(url: str, workdir: Path, max_minutes: int) -> Path:
    out_template = str(workdir / "source.%(ext)s")
    ydl_opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        duration_min = (info.get("duration") or 0) / 60.0
        if duration_min > max_minutes:
            raise ValueError(
                f"مدة الفيديو ({duration_min:.1f} دقيقة) أطول من الحد الأقصى المسموح ({max_minutes} دقيقة)."
            )
        ydl.download([url])

    for f in workdir.glob("source.*"):
        if f.suffix.lower() in (".mp4", ".mkv", ".webm"):
            return f
    raise FileNotFoundError("تعذّر العثور على ملف الفيديو بعد التنزيل.")


def extract_audio(video_path: Path, workdir: Path) -> Path:
    audio_path = workdir / "audio.mp3"
    run_cmd(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
            str(audio_path),
        ],
        "استخراج الصوت",
    )
    return audio_path


def split_audio_if_needed(audio_path: Path, workdir: Path, max_mb: float = 24.0, chunk_seconds: int = 600):
    size_mb = audio_path.stat().st_size / (1024 * 1024)
    if size_mb <= max_mb:
        return [(audio_path, 0.0)]

    chunks_dir = workdir / "audio_chunks"
    chunks_dir.mkdir(exist_ok=True)
    pattern = str(chunks_dir / "chunk_%03d.mp3")
    run_cmd(
        [
            "ffmpeg", "-y", "-i", str(audio_path),
            "-f", "segment", "-segment_time", str(chunk_seconds),
            "-ac", "1", "-ar", "16000", "-b:a", "64k",
            pattern,
        ],
        "تقسيم الصوت",
    )
    chunk_files = sorted(chunks_dir.glob("chunk_*.mp3"))
    return [(f, i * chunk_seconds) for i, f in enumerate(chunk_files)]


def transcribe_with_groq(audio_path: Path, api_key: str, offset: float = 0.0):
    client = Groq(api_key=api_key)
    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            file=(audio_path.name, f.read()),
            model="whisper-large-v3",
            response_format="verbose_json",
            language="ar",
        )
    segments = []
    raw_segments = getattr(result, "segments", None) or result.get("segments", [])
    for seg in raw_segments:
        seg = seg if isinstance(seg, dict) else seg.__dict__
        segments.append(
            {
                "start": float(seg["start"]) + offset,
                "end": float(seg["end"]) + offset,
                "text": seg["text"].strip(),
            }
        )
    return segments


def transcribe_full(audio_path: Path, workdir: Path, api_key: str, progress_cb=None):
    chunks = split_audio_if_needed(audio_path, workdir)
    all_segments = []
    for i, (chunk_path, offset) in enumerate(chunks):
        if progress_cb:
            progress_cb(i, len(chunks))
        segs = transcribe_with_groq(chunk_path, api_key, offset=offset)
        all_segments.extend(segs)
    return all_segments


def build_transcript_text(segments) -> str:
    lines = []
    for seg in segments:
        lines.append(f"[{seg['start']:.1f}s -> {seg['end']:.1f}s] {seg['text']}")
    return "\n".join(lines)


def extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text.strip())
    text = re.sub(r"```$", "", text.strip())
    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if not match:
        raise ValueError("لم يتم العثور على JSON صالح في رد النموذج.")
    return json.loads(match.group(1))


CLIP_SELECTION_PROMPT = """أنت خبير في صناعة محتوى Shorts/Reels قصير الشكل.
لديك نص مفرّغ لفيديو يوتيوب طويل مع طوابع زمنية بالثواني بالصيغة [بداية -> نهاية] نص.

المطلوب: اختر {num_clips} مقطعاً هي الأكثر جاذبية وقابلية للانتشار (لحظات مثيرة، جمل صادمة، نصائح قوية، نقاط تحول في الكلام).
كل مقطع يجب أن تكون مدته قريبة من {clip_len} ثانية (يمكن أن تختلف ±15 ثانية حسب حدود الجمل الطبيعية).
- اجعل بداية كل مقطع عند بداية جملة كاملة ونهايته عند نهاية جملة كاملة (لا تقطع في منتصف الكلام).
- لا تجعل المقاطع متداخلة مع بعضها.
- رتّب النتائج حسب الأقوى جاذبية أولاً.

أعد الإجابة **فقط** بصيغة JSON على هذا الشكل، بدون أي شرح إضافي ولا أسوار كود:
[
  {{"start": 12.3, "end": 58.7, "title": "عنوان جذاب قصير بالعربية", "hook": "أول جملة قوية تُستخدم كعنوان جذب على الشاشة"}},
  ...
]

النص المفرّغ:
---
{transcript}
---
"""


def pick_clips_with_claude(segments, api_key: str, num_clips: int, clip_len: int):
    if not ANTHROPIC_AVAILABLE:
        raise RuntimeError("مكتبة anthropic غير مثبّتة.")
    client = anthropic.Anthropic(api_key=api_key)
    transcript = build_transcript_text(segments)
    prompt = CLIP_SELECTION_PROMPT.format(num_clips=num_clips, clip_len=clip_len, transcript=transcript)
    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return extract_json(text)


def pick_clips_with_llama(segments, groq_api_key: str, num_clips: int, clip_len: int):
    client = Groq(api_key=groq_api_key)
    transcript = build_transcript_text(segments)
    prompt = CLIP_SELECTION_PROMPT.format(num_clips=num_clips, clip_len=clip_len, transcript=transcript)
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=2000,
    )
    text = resp.choices[0].message.content
    return extract_json(text)


def build_srt_for_clip(segments, clip_start: float, clip_end: float, out_path: Path):
    lines = []
    idx = 1
    for seg in segments:
        if seg["end"] <= clip_start or seg["start"] >= clip_end:
            continue
        rel_start = max(seg["start"], clip_start) - clip_start
        rel_end = min(seg["end"], clip_end) - clip_start
        if rel_end <= rel_start:
            continue
        lines.append(str(idx))
        lines.append(f"{format_srt_time(rel_start)} --> {format_srt_time(rel_end)}")
        lines.append(seg["text"])
        lines.append("")
        idx += 1
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def cut_clip_moviepy(source_video: Path, start: float, end: float, out_path: Path):
    with VideoFileClip(str(source_video)) as clip:
        sub = clip.subclip(start, min(end, clip.duration))
        sub.write_videofile(
            str(out_path),
            codec="libx264",
            audio_codec="aac",
            preset="fast",
            threads=2,
            logger=None,
        )


def cut_clip_ffmpeg(source_video: Path, start: float, end: float, out_path: Path):
    duration = max(0.1, end - start)
    run_cmd(
        [
            "ffmpeg", "-y", "-ss", str(start), "-i", str(source_video),
            "-t", str(duration), "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac", str(out_path),
        ],
        "قص المقطع بـ ffmpeg",
    )


def render_vertical_clip(raw_clip: Path, srt_path: Path, out_path: Path, burn_subtitles: bool):
    vf_filters = ["scale=-2:1920", "crop=1080:1920"]

    if burn_subtitles and srt_path.exists() and srt_path.stat().st_size > 0:
        srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
        style = "FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=0,Alignment=2,MarginV=80"
        vf_filters.append(f"subtitles='{srt_escaped}':force_style='{style}'")

    vf = ",".join(vf_filters)

    run_cmd(
        [
            "ffmpeg", "-y", "-i", str(raw_clip),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            str(out_path),
        ],
        "تحويل إلى صيغة عمودية وحرق الترجمة",
    )


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s\u0600-\u06FF-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:60] or "clip"


if generate_clicked:
    if not youtube_url.strip():
        st.error("الرجاء إدخال رابط يوتيوب صالح.")
        st.stop()
    if not groq_api_key:
        st.error("الرجاء إدخال Groq API Key من الشريط الجانبي (مطلوب للتفريغ الصوتي).")
        st.stop()
    if use_claude and not anthropic_api_key:
        st.warning("لم يتم إدخال Anthropic API Key، سيتم استخدام Llama عبر Groq بدلاً من Claude.")
        use_claude = False
    if not shutil.which("ffmpeg"):
        st.error(
            "لم يتم العثور على ffmpeg على الخادم. أضف ملف packages.txt يحتوي على السطر 'ffmpeg' "
            "في جذر المستودع عند النشر على Streamlit Cloud."
        )
        st.stop()

    workdir = Path(tempfile.mkdtemp(prefix="clipper_"))
    status = st.status("جاري المعالجة...", expanded=True)

    try:
        status.update(label="📥 جاري تنزيل الفيديو من يوتيوب...")
        source_video = download_video(youtube_url, workdir, max_video_minutes)

        status.update(label="🎧 جاري استخراج الصوت...")
        audio_path = extract_audio(source_video, workdir)

        status.update(label="📝 جاري تفريغ النص عبر Groq Whisper...")

        def _progress(i, total):
            status.update(label=f"📝 تفريغ النص... (جزء {i + 1}/{total})")

        segments = transcribe_full(audio_path, workdir, groq_api_key, progress_cb=_progress)
        if not segments:
            raise RuntimeError("لم يتم استخراج أي نص من الفيديو. تأكد من وجود كلام مسموع فيه.")

        status.update(label="🤖 جاري تحليل النص واختيار أفضل اللحظات...")
        if use_claude:
            clips = pick_clips_with_claude(segments, anthropic_api_key, num_clips, clip_len)
        else:
            clips = pick_clips_with_llama(segments, groq_api_key, num_clips, clip_len)

        if not clips:
            raise RuntimeError("لم يتمكن النموذج من اقتراح أي مقاطع.")

        results = []
        for i, clip in enumerate(clips[:num_clips]):
            status.update(label=f"✂️ جاري تجهيز المقطع {i + 1}/{min(num_clips, len(clips))}...")
            start = float(clip["start"])
            end = float(clip["end"])
            title = clip.get("title", f"مقطع {i + 1}")

            raw_clip_path = workdir / f"raw_{i}.mp4"
            try:
                if MOVIEPY_AVAILABLE:
                    cut_clip_moviepy(source_video, start, end, raw_clip_path)
                else:
                    cut_clip_ffmpeg(source_video, start, end, raw_clip_path)
            except Exception:
                cut_clip_ffmpeg(source_video, start, end, raw_clip_path)

            srt_path = workdir / f"sub_{i}.srt"
            build_srt_for_clip(segments, start, end, srt_path)

            final_name = f"{i + 1:02d}_{sanitize_filename(title)}.mp4"
            final_path = workdir / final_name
            render_vertical_clip(raw_clip_path, srt_path, final_path, burn_subs)

            results.append({"path": final_path, "title": title, "hook": clip.get("hook", "")})

        status.update(label="✅ اكتمل التوليد بنجاح!", state="complete")

        st.success(f"تم إنشاء {len(results)} مقطعاً بنجاح 🎉")
        for i, r in enumerate(results):
            st.subheader(f"{i + 1}. {r['title']}")
            if r["hook"]:
                st.caption(f"💡 {r['hook']}")
            st.video(str(r["path"]))
            with open(r["path"], "rb") as f:
                st.download_button(
                    label="⬇️ تنزيل المقطع",
                    data=f.read(),
                    file_name=r["path"].name,
                    mime="video/mp4",
                    use_container_width=True,
                    key=f"dl_{i}",
                )
            st.divider()

    except Exception as e:
        status.update(label="❌ حدث خطأ", state="error")
        st.error(f"حدث خطأ أثناء المعالجة:\n\n{e}")

    finally:
        pass


with st.expander("ℹ️ ملاحظات مهمة"):
    st.markdown(
        """
- تحتاج إلى **Groq API Key** مجاني من console.groq.com لتفعيل التفريغ الصوتي.
- Claude اختياري: إن لم تُدخل مفتاحه سيُستخدم نموذج **Llama 3.3** عبر Groq مجاناً بديلاً لاختيار المقاطع.
- عند النشر على **Streamlit Community Cloud** تأكد من وجود ملف `packages.txt` بجذر المستودع يحتوي على السطر `ffmpeg`.
- الفيديوهات الطويلة جداً قد تستغرق وقتاً أطول في التنزيل والتفريغ والمعالجة.
        """
  )
