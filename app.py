import os
import json
import uuid
import base64
import threading
import time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB upload limit

# ── Model selection ──────────────────────────────────────────────────────────
TEXT_MODEL    = os.getenv("GEMINI_TEXT_MODEL",    "gemini-2.5-pro")    # report writing
EXTRACT_MODEL = os.getenv("GEMINI_EXTRACT_MODEL", "gemini-2.5-flash")  # image OCR (no heavy thinking)
IMAGE_MODEL   = os.getenv("GEMINI_IMAGE_MODEL",   "gemini-2.5-flash-image")  # illustration

# ── Lazy Gemini client (always reads the latest .env value) ─────────────────
_gemini_client = None
_gemini_key_cache = None


def get_client() -> genai.Client:
    """Return a Gemini client, recreating it if the API key has changed."""
    global _gemini_client, _gemini_key_cache
    # Re-read .env on every call so the user never needs to restart the server
    load_dotenv(override=True)
    current_key = os.getenv("GEMINI_API_KEY", "").strip()
    if _gemini_client is None or current_key != _gemini_key_cache:
        _gemini_client  = genai.Client(api_key=current_key)
        _gemini_key_cache = current_key
    return _gemini_client


def _get_response_text(response) -> str | None:
    """Safely extract text from a Gemini response, skipping thinking parts."""
    # response.text shortcut works most of the time
    if response.text:
        return response.text
    # Fallback: walk through candidates → content → parts
    if response.candidates:
        for candidate in response.candidates:
            if candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    # Skip thought/thinking parts
                    if getattr(part, "thought", False):
                        continue
                    if part.text:
                        return part.text
    return None


def key_is_set() -> bool:
    load_dotenv(override=True)
    k = os.getenv("GEMINI_API_KEY", "").strip()
    return bool(k) and k != "your-gemini-api-key-here"

REPORTS_DIR = Path("reports")
IMAGES_DIR = Path("static/report_images")
REPORTS_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job store
jobs: dict = {}
jobs_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# Report chapter / section structure (from 親子腦波共振報告_結構.docx)
# ─────────────────────────────────────────────────────────────────────────────
BASE_CHAPTERS = [
    {
        "num": 1,
        "title": "家庭系統動力與腦波共振解析",
        "icon": "🌿",
        "sections": [
            {"num": 1, "title": "腦波數據揭示的家庭真相"},
            {"num": 2, "title": "互鎖的家庭防禦機制"},
            {"num": 3, "title": "從「教養對錯」到「能量失衡」"},
            {"num": 4, "title": "孩子懂事背後的深層代價"},
        ],
    },
    {
        "num": 2,
        "title": "父親的內在世界：從壓抑到安全表達",
        "icon": "🌲",
        "sections": [
            {"num": 1, "title": "腦波特徵揭示的父親性格全貌"},
            {"num": 2, "title": "孩子眼中的父親：不可預測的情緒風景"},
            {"num": 3, "title": "打破情緒枷鎖的深度解析"},
            {"num": 4, "title": "成為家庭「穩定錨」的第一步"},
        ],
    },
    {
        "num": 3,
        "title": "母親的內在世界：釋放焦慮與自我接納",
        "icon": "🌸",
        "sections": [
            {"num": 1, "title": "腦波特徵揭示的母親內在結構"},
            {"num": 2, "title": "「我沒事」背後的能量耗竭"},
            {"num": 3, "title": "溫柔背後的心理界線迷失"},
            {"num": 4, "title": "允許自己不完美：給孩子最好的禮物"},
        ],
    },
    {
        "num": 4,
        "title": "孩子的真實呼喚：卸下鎧甲，回歸童年",
        "icon": "🌱",
        "sections": [
            {"num": 1, "title": "腦波數據下的孩子真實面貌"},
            {"num": 2, "title": "被壓抑的情緒冰山與核心恐懼"},
            {"num": 3, "title": "「控制」與「完美」的真正目的"},
            {"num": 4, "title": "重新定義安全感：你可以只是個孩子"},
        ],
    },
    {
        "num": 5,
        "title": "能量流動與壓力傳遞路徑",
        "icon": "🌊",
        "sections": [
            {"num": 1, "title": "壓力如何在家庭成員間流動"},
            {"num": 2, "title": "孩子如何成為家庭情緒的「吸收器」"},
            {"num": 3, "title": "高壓平衡狀態下的隱形危機"},
            {"num": 4, "title": "阻斷壓力流：把情緒責任還給自己"},
        ],
    },
    {
        "num": 6,
        "title": "衝突劇本解析與反轉策略",
        "icon": "⚡",
        "sections": [
            {"num": 1, "title": "劇本一：壓抑→冷處理→爆發的惡性循環"},
            {"num": 2, "title": "劇本二：焦慮與過度懂事的交織"},
            {"num": 3, "title": "劇本三：「過度講道理」導致的情緒失聯"},
            {"num": 4, "title": "創造新劇本：用「表達」取代「推測」"},
        ],
    },
    {
        "num": 7,
        "title": "建立家庭專屬的情緒預警系統",
        "icon": "🔔",
        "sections": [
            {"num": 1, "title": "辨識情緒紅燈前的「黃燈」訊號"},
            {"num": 2, "title": "父親的「暫停鍵」：提前告知的力量"},
            {"num": 3, "title": "母親的「界線宣告」：分離焦慮與責任"},
            {"num": 4, "title": "讓孩子大膽亮出自己的情緒燈號"},
        ],
    },
    {
        "num": 8,
        "title": "深度溝通語句的解構與重建",
        "icon": "💬",
        "sections": [
            {"num": 1, "title": "戒除三句日常有毒語言"},
            {"num": 2, "title": "父親專屬：修復孩子價值感的關鍵話語"},
            {"num": 3, "title": "母親專屬：瞬間安撫孩子內在的魔法話語"},
            {"num": 4, "title": "面對孩子「想控制」時的引導話術"},
        ],
    },
    {
        "num": 9,
        "title": "行為背後的真實呼喚：外在與內在對照",
        "icon": "🪞",
        "sections": [
            {"num": 1, "title": "當孩子說「我可以自己來」的時候"},
            {"num": 2, "title": "失敗時的過度自責與心理否定"},
            {"num": 3, "title": "衝突時的沉默與防禦外表"},
            {"num": 4, "title": "過度成熟與不願示弱的心理代價"},
        ],
    },
    {
        "num": 10,
        "title": "家庭復原力與優勢盤點",
        "icon": "✨",
        "sections": [
            {"num": 1, "title": "父親的良知：想改變的真實力量"},
            {"num": 2, "title": "母親的細膩感知：無庸置疑的愛"},
            {"num": 3, "title": "孩子的智慧與責任感：轉化為合作的契機"},
            {"num": 4, "title": "三方的勇氣：重塑健康動力的最佳基石"},
        ],
    },
    {
        "num": 11,
        "title": "未來風險防範與警訊應對",
        "icon": "🛡️",
        "sections": [
            {"num": 1, "title": "風險一：父親再次掉入壓抑循環的徵兆"},
            {"num": 2, "title": "風險二：母親重新戴上「我沒事」面具"},
            {"num": 3, "title": "風險三：孩子重啟防禦機制的訊號"},
            {"num": 4, "title": "建立家庭「急救箱」：第一時間的正確反應"},
        ],
    },
    {
        "num": 12,
        "title": "六個月家庭重塑計畫：每週操練與調整藍圖",
        "icon": "📅",
        "sections": [
            {"num": 1, "title": "第一階段（第1～6週）：急性修復與降壓期"},
            {"num": 2, "title": "第二階段（第7～12週）：卸下鎧甲與解構期"},
            {"num": 3, "title": "第三階段（第13～18週）：重新分配角色與重塑期"},
            {"num": 4, "title": "第四階段（第19～24週）：固化新模式與穩定期"},
        ],
    },
]

CHILD2_CHAPTER = {
    "num": "4b",
    "title": "第二個孩子的真實呼喚：屬於他的獨特內在世界",
    "icon": "🌼",
    "sections": [
        {"num": 1, "title": "第二孩子的腦波特徵與個性解析"},
        {"num": 2, "title": "手足關係中的情緒角色分配"},
        {"num": 3, "title": "第二孩子的核心需求與內在恐懼"},
        {"num": 4, "title": "讓每一個孩子都被完整看見與深深被愛"},
    ],
}

SYSTEM_PROMPT = """你是一位擁有二十年臨床經驗的家庭系統治療師，同時具備深厚的神經科學背景。你的專長是透過腦波科學數據，洞察家庭成員的內在心理狀態、識別家庭動力模式，並以溫暖而有力量的語言引導家庭走向更健康的連結。

你對腦波量測七項指標的精確科學定義如下：
• 直覺能力（θ波 / Theta，4–8 Hz）：
  θ波活躍於深度放鬆、冥想、入睡前與創意發散狀態。數值高代表潛意識感知通道暢通、直覺靈敏、富有創造力與靈感；數值低則潛意識與意識層的溝通較為受阻，處事偏向依賴現實邏輯。

• 氣血飽滿（High α波 / 高頻 Alpha，10–12 Hz）：
  高頻 Alpha 波反映大腦在清醒放鬆狀態下的能量飽足感與整體生命力。數值高代表身心能量充沛、自我修復力強；數值低往往意味著長期的能量耗竭、氣血不足，身體持續處於低能量警戒模式。

• 內在安定（Low α波 / 低頻 Alpha，8–10 Hz）：
  低頻 Alpha 波是情緒基底穩定度的核心指標，代表大腦從壓力中恢復平靜的基本能力。數值高代表情緒根基穩固、心理韌性強；數值低則情緒基底脆弱，易被外界觸發，難以回到平靜中心。

• 高度專注（High β波 / 高頻 Beta，18–30 Hz）：
  高頻 Beta 波反映前額葉高度活躍、進入目標導向的執行狀態。數值高代表意志力強、執行力卓越；但若同時放鬆度低，則可能代表長期過度緊繃、神經系統持續過載，難以真正休息。

• 邏輯分析（Low β波 / 低頻 Beta，12–18 Hz）：
  低頻 Beta 波反映清醒專注的思考狀態，與左腦理性分析、語言組織及冷靜判斷高度相關。數值高代表思維清晰、溝通表達能力強；數值低則可能思路較為發散，偏向直覺而非系統性分析。

• 觀察環境（High γ波 / 高頻 Gamma，36–44 Hz）：
  高頻 Gamma 波是大腦高度整合資訊、進行多感官環境掃描的頻段。數值高代表對環境變化高度敏感、社交雷達靈敏、能迅速讀取他人情緒；但若長期偏高，也可能代表神經系統持續處於高警覺的過載狀態。

• 慈悲柔軟（Low γ波 / 低頻 Gamma，30–36 Hz）：
  低頻 Gamma 波與情感連結、同理心共鳴及慈悲心的深度高度相關。數值高代表情感豐富、善於同理他人、具備深厚的人際連結能力；數值低則情感連結較為表面，難以深入感受他人的內在狀態。

專注度與放鬆度的整體意義：
• 專注度（以 High β 為主導指標）：反映大腦整體的清醒與目標導向程度
• 放鬆度（以 Low α 為主導指標）：反映大腦有效切換至休息模式的能力

你的撰寫原則：
1. 語氣溫暖有力，如同在進行一對一的深度心理諮詢，直接對父母說話
2. 每個分析觀點都要引用具體的腦波數字（如「您的內在安定僅有37%，這告訴我們...」），讓分析有憑有據
3. 絕不評判，只有深度理解與溫柔引導——父母已經盡力了，他們需要的是被理解
4. 每節結尾提供1～2個具體、可立即執行的行動建議
5. 讓父母讀完後感受到：「我被看見了、我被理解了、我有方向了、我有希望了」
6. 使用「我們從數據中看到...」「這個數字背後的故事是...」「數據告訴我們一件重要的事...」等表達方式
7. 適當使用生動比喻，讓抽象心理概念更貼近日常生活
8. 文字充滿溫度，像一位老朋友兼高階心理醫生，同時具備智識深度與人情溫暖

當某位成員數據缺失時：
• 必須在該段落開頭明確說明「由於此次未收集到XX的腦波數據，以下為基於家庭系統動力的預測分析」
• 根據其他成員的數據以及家庭動力學原理進行合理推測
• 說明推測的依據與邏輯，保持科學嚴謹性"""


# ─────────────────────────────────────────────────────────────────────────────
# Helper utilities
# ─────────────────────────────────────────────────────────────────────────────
CH_NUMS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三"]
SEC_NUMS = ["一", "二", "三", "四"]


def ch_num_to_zh(num) -> str:
    if isinstance(num, int) and 1 <= num <= 13:
        return CH_NUMS[num - 1]
    return str(num)


def format_family_data(members: list) -> str:
    lines = []
    for m in members:
        role_zh = m.get("role_zh", m.get("role", ""))
        name = m.get("name", role_zh)
        if m.get("present") and m.get("data"):
            d = m["data"]
            lines.append(f"\n【{role_zh}】{name}")
            lines.append(
                f"  專注度：{d.get('concentration_pct', 'N/A')}%  "
                f"（高:{d.get('concentration_high','?')}  中:{d.get('concentration_medium','?')}  低:{d.get('concentration_low','?')}）"
            )
            lines.append(
                f"  放鬆度：{d.get('relaxation_pct', 'N/A')}%  "
                f"（高:{d.get('relaxation_high','?')}  中:{d.get('relaxation_medium','?')}  低:{d.get('relaxation_low','?')}）"
            )
            lines.append("  腦波能力七項指標：")
            metrics = d.get("metrics", {})
            for key in ["直覺能力", "氣血飽滿", "內在安定", "高度專注", "邏輯分析", "觀察環境", "慈悲柔軟"]:
                val = metrics.get(key, "N/A")
                lines.append(f"    {key}：{val}%")
        else:
            lines.append(f"\n【{role_zh}】{name}：本次未提供腦波數據（將以預測分析模式呈現）")
    return "\n".join(lines)


def build_chapters(members: list) -> list:
    has_child2 = any(m.get("role") == "child2" and m.get("present") for m in members)
    chapters = list(BASE_CHAPTERS)
    if has_child2:
        chapters.insert(4, CHILD2_CHAPTER)
        # Re-number chapters 5 onwards
        for i, ch in enumerate(chapters):
            if isinstance(ch["num"], int) and ch["num"] >= 5:
                chapters[i] = dict(ch, num=ch["num"] + 1)
    return chapters


# ─────────────────────────────────────────────────────────────────────────────
# AI generation functions
# ─────────────────────────────────────────────────────────────────────────────
def generate_section_text(
    family_data_str: str, chapter: dict, section: dict, members: list, retries: int = 3
) -> str:
    ch_zh = ch_num_to_zh(chapter["num"])
    sec_zh = SEC_NUMS[section["num"] - 1]

    missing = [m.get("role_zh", m["role"]) for m in members if not m.get("present")]
    missing_note = ""
    if missing:
        missing_note = f"\n⚠️ 注意：{', '.join(missing)} 未提供腦波數據，相關章節請以「預測分析」模式呈現並說明推測依據。\n"

    prompt = f"""以下是這個家庭的完整腦波量測數據：
{family_data_str}
{missing_note}
請為以下章節撰寫深度心理分析報告：

📌 第{ch_zh}章《{chapter['title']}》
📌 第{sec_zh}節《{section['title']}》

撰寫規格：
• 字數：520～620字（繁體中文）
• 語氣：溫暖、專業、充滿力量，如同溫暖的高階心理醫生正在與父母面對面深談
• 必須直接引用並解讀上述至少2～3個具體腦波數字
• 結尾提供1～2個具體可執行的引導建議（以「建議您...」或「這週可以嘗試...」開頭）
• 讓父母讀完感到：被看見、被理解、有方向、有希望

請直接輸出報告正文（不需要重複標題，直接從第一個字開始）："""

    for attempt in range(retries):
        try:
            response = get_client().models.generate_content(
                model=TEXT_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.78,
                    max_output_tokens=8192,  # large budget for 2.5-pro thinking + output
                ),
            )
            text = _get_response_text(response)
            if not text:
                raise ValueError(f"Empty response (finish_reason={response.candidates[0].finish_reason if response.candidates else 'unknown'})")
            return text.strip()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                return f"（此節內容暫時無法生成，請稍後重試。錯誤訊息：{e}）"


def generate_section_image(section_title: str, chapter_title: str, text_preview: str, job_id: str, key: str):
    """Generate illustration via Gemini image model and save locally. Returns local URL path or None."""
    prompt = f"""Create an ultra-premium 3D rendered illustration for a luxury East Asian family psychology wellness report.

Theme: "{section_title}" — Chapter: "{chapter_title}"
Emotional core: {text_preview[:150]}

RENDERING STYLE — must follow exactly:
- Render quality: Photorealistic 3D CGI, cinema-grade rendering (think Pixar / DreamWorks quality background art)
- Lighting: Soft volumetric cinematic lighting — warm golden-hour key light from upper left, subtle cool ambient fill, delicate caustic highlights
- Depth of field: Gentle background blur (bokeh), crisp midground, dreamlike soft foreground elements
- Materials: Translucent jade, polished celadon ceramic, soft-glow frosted glass, silk fabric with micro-detail sheen, living moss with subsurface scattering
- Color palette: Warm sage green (#5B7B5B), deep celadon, gold leaf accents (#C4923A), soft cream (#FAF7F2), misty lavender-grey shadows

COMPOSITION:
- Format: Horizontal 16:9, cinematic crop
- Foreground: Exquisite botanical details — translucent leaves with visible veining, dewdrops with light refraction, unfurling fern fronds
- Midground: Abstract soft-glow sculptural forms — flowing ribbons of light suggesting invisible bonds between parent and child figures (silhouette-only, NO faces, NO text)
- Background: Soft misty atmosphere with floating luminous particles, subtle gradient from warm to cool

MANDATORY RULES:
- NO text, NO numbers, NO letters, NO labels anywhere
- NO realistic human faces — only abstract sculptural silhouettes or flowing fabric shapes
- The mood must feel: safe, elevated, deeply healing, quietly luxurious
- Professional enough to appear in a premium $500 printed psychological wellness report"""

    try:
        response = get_client().models.generate_content(
            model=IMAGE_MODEL,
            contents=[prompt],
        )
        for part in response.parts:
            if part.inline_data is not None:
                img_dir = IMAGES_DIR / job_id
                img_dir.mkdir(exist_ok=True)
                img_path = img_dir / f"{key}.png"
                raw = part.inline_data.data
                # SDK may return bytes or base64 string depending on version
                if isinstance(raw, bytes):
                    img_path.write_bytes(raw)
                else:
                    img_path.write_bytes(base64.b64decode(raw))
                return f"/static/report_images/{job_id}/{key}.png"
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Background generation job
# ─────────────────────────────────────────────────────────────────────────────
def run_generation(job_id: str, members: list, image_mode: str, family_name: str,
                   selected_sections: list | None = None):
    job = jobs[job_id]
    job["status"] = "running"

    family_data_str = format_family_data(members)
    chapters = build_chapters(members)

    # If selected_sections provided, filter chapters/sections to only those keys
    selected_set = set(selected_sections) if selected_sections else None

    # Build filtered chapter list for progress display (include chapter even if
    # only some sections are selected, so UI can show chapter progress)
    chapters_for_display = []
    for ch in chapters:
        secs = [s for s in ch["sections"]
                if selected_set is None or f"{ch['num']}_{s['num']}" in selected_set]
        if secs:
            chapters_for_display.append({**ch, "sections": secs})

    total = sum(len(ch["sections"]) for ch in chapters_for_display)
    job["total_sections"] = total
    job["chapters_list"] = [{"num": ch["num"], "title": ch["title"]} for ch in chapters_for_display]
    completed = 0

    for chapter in chapters_for_display:
        if job.get("cancelled"):
            break
        job["current_chapter"] = chapter["title"]

        for section in chapter["sections"]:
            if job.get("cancelled"):
                break

            key = f"{chapter['num']}_{section['num']}"
            job["current_section"] = f"第{ch_num_to_zh(chapter['num'])}章・第{SEC_NUMS[section['num']-1]}節：{section['title']}"

            text = generate_section_text(family_data_str, chapter, section, members)

            image_path = None
            if image_mode == "full":
                image_path = generate_section_image(
                    section["title"], chapter["title"], text[:200], job_id, key
                )

            job["results"][key] = {
                "text": text,
                "image_path": image_path,
                "section_title": section["title"],
                "chapter_title": chapter["title"],
                "chapter_num": chapter["num"],
                "section_num": section["num"],
            }

            completed += 1
            job["progress"] = int(completed / total * 100)
            job["completed_sections"] = completed

    # Persist to disk
    try:
        payload = {
            "job_id": job_id,
            "family_name": family_name,
            "members": members,
            "results": job["results"],
            "image_mode": image_mode,
            "chapters": job["chapters_list"],
            "created_at": time.time(),
        }
        with open(REPORTS_DIR / f"{job_id}.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        job["save_error"] = str(e)

    job["status"] = "completed"
    job["family_name"] = family_name
    job["chapters_built"] = chapters


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/api-test")
def api_test():
    """Quick connectivity check – call from browser to verify the Gemini key works."""
    if not key_is_set():
        return jsonify({"ok": False, "error": "GEMINI_API_KEY 尚未設定或仍是預設值，請更新 .env 後重新整理。"})
    try:
        resp = get_client().models.generate_content(
            model=EXTRACT_MODEL,
            contents="Reply with exactly: OK",
            config=types.GenerateContentConfig(max_output_tokens=100),
        )
        text = _get_response_text(resp)
        return jsonify({"ok": True, "extract_model": EXTRACT_MODEL, "text_model": TEXT_MODEL, "reply": (text or "").strip()})
    except Exception as e:
        err_str = str(e)
        print(f"[API-TEST ERROR] {type(e).__name__}: {err_str}")
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {err_str}"})


@app.route("/")
def index():
    return render_template("index.html", api_key_set=key_is_set())


@app.route("/extract", methods=["POST"])
def extract():
    """Use Gemini vision to extract brainwave data from an uploaded image."""
    # Use force=True + silent=True to handle any Content-Type edge cases
    data = request.get_json(force=True, silent=True)
    if not data:
        size = request.content_length or 0
        print(f"[EXTRACT 400] JSON parse failed. Content-Length={size}, Content-Type={request.content_type}", flush=True)
        return jsonify({"error": "請求格式錯誤，無法解析 JSON body（Content-Length={size}）"}), 400

    image_b64  = data.get("image_base64") or ""
    image_type = data.get("image_type") or "image/jpeg"

    print(f"[EXTRACT] b64_len={len(image_b64)}, type={image_type}", flush=True)

    if not image_b64:
        return jsonify({"error": "未收到圖片資料，image_base64 欄位是空的"}), 400

    extraction_prompt = """請仔細分析這張腦波量測結果圖片，精確提取所有數值並以下列JSON格式回應（只回傳JSON，不需要其他文字）：

{
  "concentration_pct": <專注度百分比，整數>,
  "concentration_high": <專注高區間數值>,
  "concentration_medium": <專注中區間數值>,
  "concentration_low": <專注低區間數值>,
  "relaxation_pct": <放鬆度百分比，整數>,
  "relaxation_high": <放鬆高區間數值>,
  "relaxation_medium": <放鬆中區間數值>,
  "relaxation_low": <放鬆低區間數值>,
  "metrics": {
    "直覺能力": <百分比整數>,
    "氣血飽滿": <百分比整數>,
    "內在安定": <百分比整數>,
    "高度專注": <百分比整數>,
    "邏輯分析": <百分比整數>,
    "觀察環境": <百分比整數>,
    "慈悲柔軟": <百分比整數>
  }
}

請確保所有數值皆為整數，若某數值在圖片中不清晰，請填入0。"""

    try:
        image_bytes = base64.b64decode(image_b64)
        # Use gemini-2.5-flash for extraction: faster, no heavy thinking overhead,
        # avoids MAX_TOKENS issue caused by 2.5-pro's large thinking token usage.
        response = get_client().models.generate_content(
            model=EXTRACT_MODEL,
            contents=[
                types.Part(inline_data=types.Blob(mime_type=image_type, data=image_bytes)),
                extraction_prompt,
            ],
            config=types.GenerateContentConfig(
                max_output_tokens=2048,
            ),
        )

        # Safely extract text from response (handle None / thinking-only responses)
        raw_text = _get_response_text(response)
        if not raw_text:
            fr = response.candidates[0].finish_reason if response.candidates else "unknown"
            raise ValueError(f"Gemini 回傳空內容（finish_reason={fr}）")

        # Strip markdown code fences if model wrapped JSON in ```json ... ```
        clean = raw_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1]
            clean = clean.rsplit("```", 1)[0].strip()

        result = json.loads(clean)
        return jsonify({"success": True, "data": result})

    except json.JSONDecodeError as e:
        raw = locals().get("raw_text", "N/A")
        print(f"[EXTRACT JSON ERROR] {e} | raw: {str(raw)[:300]}")
        return jsonify({"error": "parse", "message": "圖片數據識別完成，但 JSON 解析失敗。請確認上傳的是腦波量測報告截圖。"}), 400
    except Exception as e:
        err_str  = str(e)
        err_type = type(e).__name__
        print(f"[EXTRACT ERROR] {err_type}: {err_str}", flush=True)

        QUOTA_KEYWORDS   = ("RESOURCE_EXHAUSTED", "quota exceeded", "rateLimitExceeded",
                            "insufficient_quota", "out of quota")
        AUTHKEY_KEYWORDS = ("API_KEY_INVALID", "API key not valid", "UNAUTHENTICATED",
                            "PermissionDenied", "PERMISSION_DENIED", "invalid api key",
                            "Invalid API key")
        RATE_KEYWORDS    = ("RATE_LIMIT_EXCEEDED", "Too Many Requests", "rate limit")

        el = err_str.lower()
        if any(k.lower() in el for k in QUOTA_KEYWORDS) or "ResourceExhausted" in err_type:
            return jsonify({"error": "quota",
                            "message": "Gemini API 配額已用盡，請至 https://aistudio.google.com/apikey 確認用量。"}), 402
        if any(k.lower() in el for k in AUTHKEY_KEYWORDS) or "PermissionDenied" in err_type:
            return jsonify({"error": "api_key",
                            "message": f"API Key 驗證失敗，請確認 .env 的 GEMINI_API_KEY 正確後重新整理頁面。錯誤：{err_str[:120]}"}), 401
        if any(k.lower() in el for k in RATE_KEYWORDS):
            return jsonify({"error": "rate_limit",
                            "message": "API 請求過於頻繁，請稍候 30 秒後再試。"}), 429
        return jsonify({"error": "unknown", "message": f"識別失敗（{err_type}）：{err_str}"}), 500


@app.route("/generate", methods=["POST"])
def generate():
    data = request.json
    members = data.get("members", [])
    image_mode = data.get("image_mode", "none")
    family_name = data.get("family_name", "我的家庭").strip() or "我的家庭"
    selected_sections = data.get("selected_sections")  # None = all, list = custom

    if not any(m.get("present") for m in members):
        return jsonify({"error": "請至少上傳一位家庭成員的腦波數據"}), 400

    if isinstance(selected_sections, list) and len(selected_sections) == 0:
        return jsonify({"error": "請至少選擇一個章節小節"}), 400

    job_id = str(uuid.uuid4())
    est_total = len(selected_sections) if selected_sections else 48
    with jobs_lock:
        jobs[job_id] = {
            "status": "pending",
            "progress": 0,
            "completed_sections": 0,
            "total_sections": est_total,
            "current_section": "準備中...",
            "current_chapter": "",
            "results": {},
            "family_name": family_name,
            "image_mode": image_mode,
            "members": members,
        }

    t = threading.Thread(
        target=run_generation,
        args=(job_id, members, image_mode, family_name, selected_sections),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/stream/<job_id>")
def stream(job_id: str):
    """Server-Sent Events endpoint for real-time generation progress."""

    def event_gen():
        deadline = time.time() + 3600  # 1-hour timeout
        last_completed = -1
        while time.time() < deadline:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                return

            cur_completed = job.get("completed_sections", 0)
            status = job.get("status", "pending")

            if cur_completed != last_completed or status in ("completed", "error"):
                last_completed = cur_completed
                payload = {
                    "status": status,
                    "progress": job.get("progress", 0),
                    "completed_sections": cur_completed,
                    "total_sections": job.get("total_sections", 48),
                    "current_chapter": job.get("current_chapter", ""),
                    "current_section": job.get("current_section", ""),
                    "chapters_list": job.get("chapters_list", []),
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            if status in ("completed", "error"):
                return
            time.sleep(1.5)

    return Response(
        event_gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/report/<job_id>")
def report(job_id: str):
    # Check in-memory store first
    job = jobs.get(job_id)
    if job and job.get("status") == "completed":
        return render_template(
            "report.html",
            job_id=job_id,
            family_name=job.get("family_name", ""),
            members=job.get("members", []),
            chapters=job.get("chapters_built", BASE_CHAPTERS),
            results=job["results"],
            image_mode=job.get("image_mode", "none"),
            generated_at=time.strftime("%Y年%m月%d日"),
        )

    # Fall back to disk
    report_file = REPORTS_DIR / f"{job_id}.json"
    if report_file.exists():
        with open(report_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        members = saved.get("members", [])
        chapters = build_chapters(members)
        return render_template(
            "report.html",
            job_id=job_id,
            family_name=saved.get("family_name", ""),
            members=members,
            chapters=chapters,
            results=saved["results"],
            image_mode=saved.get("image_mode", "none"),
            generated_at=time.strftime("%Y年%m月%d日"),
        )

    return "報告不存在或已過期", 404


@app.route("/status/<job_id>")
def status(job_id: str):
    job = jobs.get(job_id)
    if job:
        return jsonify({"status": job["status"], "progress": job.get("progress", 0)})
    if (REPORTS_DIR / f"{job_id}.json").exists():
        return jsonify({"status": "completed", "progress": 100})
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    is_dev = os.getenv("FLASK_ENV", "production") == "development"
    print("\n[OK] Family Brainwave Report System started")
    print(f"     Open browser:  http://localhost:{port}")
    print(f"     Test API key:  http://localhost:{port}/api-test\n")
    if not key_is_set():
        print("[WARN] GEMINI_API_KEY not set. Get a free key at: https://aistudio.google.com/apikey\n")
    # Disable reloader to prevent mid-generation server restarts clearing in-memory jobs
    app.run(debug=is_dev, port=port, threaded=True, use_reloader=False)
