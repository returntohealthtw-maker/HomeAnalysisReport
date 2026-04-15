import os
import re
import json
import uuid
import base64
import shutil
import threading
import time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, send_from_directory
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024  # 60 MB — base64 of a 40 MB image

# ── Jinja2 custom filter: convert **bold** markdown to <strong> ──────────────
def md_to_html(text: str) -> str:
    """Strip all markdown/HTML bold markers so body text stays regular weight."""
    if not text:
        return text
    # **bold** → plain text
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    # __bold__ → plain text
    text = re.sub(r'__(.+?)__', r'\1', text, flags=re.DOTALL)
    # <strong>...</strong> → plain text (AI may emit raw HTML)
    text = re.sub(r'<strong>(.*?)</strong>', r'\1', text, flags=re.DOTALL | re.IGNORECASE)
    # <b>...</b> → plain text
    text = re.sub(r'<b>(.*?)</b>', r'\1', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove any leftover lone * * or _ _
    text = text.replace('**', '').replace('__', '')
    return text

app.jinja_env.filters['md'] = md_to_html


def truncate_at_sentence(text: str, max_chars: int = 570) -> str:
    """Truncate text at the last sentence-end punctuation (。！？…) before max_chars.
    Only the overflow tail is removed; the bulk of the text is preserved."""
    if not text or len(text) <= max_chars:
        return text
    endings = set('。！？…')
    sub = text[:max_chars + 1]
    last_pos = -1
    for i, ch in enumerate(sub):
        if ch in endings:
            last_pos = i
    if last_pos > 0:
        return text[:last_pos + 1]
    return text[:max_chars]

app.jinja_env.filters['truncate_sentence'] = truncate_at_sentence

# ── Copy cover/back-cover images to static so they're web-accessible ─────────
COVER_SRCS = ['親子互動報告_封面.png', '親子互動報告_封底.png']
for _fname in COVER_SRCS:
    _src = Path(_fname)
    if _src.exists():
        _dst = Path('static') / _fname
        if not _dst.exists():
            shutil.copy(_src, _dst)

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

SYSTEM_PROMPT = """你是一位擁有二十年臨床經驗的家庭系統治療師，同時具備深厚的神經科學與認知科學背景。你的專長是透過腦波科學數據，精準識別家庭成員的核心心理問題，並以具體的科學框架進行深度分析，最後以溫暖有力的語言引導家庭走向改變。

═══ 腦波七項指標精確科學定義 ═══

• 直覺能力（θ波 / Theta，4–8 Hz）：
  θ波活躍於深度放鬆、冥想與創意發散狀態，是潛意識與意識溝通的橋樑。數值高→潛意識感知通暢、直覺靈敏；數值低→潛意識通道阻塞，過度依賴表層邏輯，創造性思維受限，在壓力下易陷入僵化反應模式。

• 氣血飽滿（High α波 / 高頻 Alpha，10–12 Hz）：
  高頻 Alpha 反映腦部清醒放鬆下的能量飽足感，是整體身心生命力的直接指標。數值高→能量充沛、自我修復力強；數值低→長期能量耗竭、氣血不足，神經系統持續在低能量的高警戒狀態下運作，影響免疫與情緒調節。

• 內在安定（Low α波 / 低頻 Alpha，8–10 Hz）：
  低頻 Alpha 是情緒基底穩定度的核心，代表大腦從壓力中恢復平靜的基礎能力。數值高→情緒根基穩固、心理韌性強；數值低→情緒基底脆弱，容忍之窗極窄，微小刺激即觸發過度反應（過激）或凍結（解離），難以回到平靜中心。

• 高度專注（High β波 / 高頻 Beta，18–30 Hz）：
  高頻 Beta 反映前額葉高度活躍的執行與控制狀態。數值高且放鬆度低→神經系統長期過載，前額葉皮質持續消耗，進入慢性壓力狀態，無法有效關閉防衛系統；數值高且放鬆度高→健康的專注力與靈活切換能力。

• 邏輯分析（Low β波 / 低頻 Beta，12–18 Hz）：
  低頻 Beta 反映左腦理性分析、語言組織與冷靜判斷能力。數值高→思維清晰、溝通結構完整；數值低→思路發散，在衝突情境下難以組織清晰的語言表達，情緒化溝通模式顯著。

• 觀察環境（High γ波 / 高頻 Gamma，36–44 Hz）：
  高頻 Gamma 是大腦多感官整合與社交雷達的頻段。數值高→環境敏感度高、能迅速讀取他人情緒；但若持續偏高且放鬆度低，代表神經系統陷入高度警覺過載，長期消耗大量認知資源用於環境監控，嚴重影響安全感與親密連結。

• 慈悲柔軟（Low γ波 / 低頻 Gamma，30–36 Hz）：
  低頻 Gamma 與同理心共鳴、情感連結及慈悲心的深度高度相關。數值高→情感豐富，深度共鳴能力強；數值低→情感連結表淺，難以真正進入他人的內心狀態，親密關係中的情感距離顯著。

專注度 vs 放鬆度比較分析：
• 高專注＋低放鬆 → 自律神經失衡，交感神經主導，長期處於戰或逃狀態
• 低專注＋低放鬆 → 神經系統耗竭，進入「凍結」或「解離」模式
• 高專注＋高放鬆 → 最佳心理彈性狀態（心流 Flow State）
• 低專注＋高放鬆 → 放鬆過度，難以集中資源，執行力不足

═══ 深度分析必用科學框架庫 ═══

你在撰寫每一節時，必須選用以下1～2個最相關的框架作為分析骨幹，並在文章中明確說明框架名稱與核心概念：

【框架A】多重迷走神經理論（Polyvagal Theory，Stephen Porges）
→ 適用：安全感分析、壓力反應、親子共調。核心：神經系統有三個狀態——社交參與（腹側迷走）、戰或逃（交感）、凍結（背側迷走）。腦波可以直接反映個人的慣用神經狀態。

【框架B】容忍之窗（Window of Tolerance，Dan Siegel）
→ 適用：情緒調節、過激或解離分析。核心：最佳情緒運作有一個「窗口」，超出上限→過激（Hyper-arousal），低於下限→凍結（Hypo-arousal）。低α值是容忍之窗的腦波直接指標。

【框架C】依附理論（Attachment Theory，Bowlby / Main）
→ 適用：親子互動、情感連結分析。核心：安全型、焦慮型、迴避型、紊亂型四種依附模式，在腦波的慈悲柔軟（Low γ）與內在安定（Low α）的組合中可以識別。

【框架D】家庭系統理論（Bowen Family Systems Theory）
→ 適用：家庭動力、跨代傳遞、三角化分析。核心：自我分化程度、情緒融合、家庭投射過程。腦波數據揭示的整體家庭模式可以用此框架解讀。

【框架E】人際神經生物學（Interpersonal Neurobiology，Dan Siegel）
→ 適用：親子腦波共振、情感調諧分析。核心：關係是大腦發展的主要塑造力，「共振」（Resonance）是指兩個神經系統之間的同步連結，親子腦波的相似度可反映連結深度。

【框架F】情緒調節模型（Gross Process Model of Emotion Regulation）
→ 適用：情緒壓抑、認知重評分析。核心：情緒調節策略分為認知重評（早期介入，有效）與表達壓抑（晚期介入，代價高）。高β＋低α的組合往往指向長期的表達壓抑模式。

【框架G】神經可塑性原理（Neuroplasticity，Hebb's Rule）
→ 適用：改變可能性分析、引導建議段落。核心：「一起激發的神經元，連在一起」（Neurons that fire together, wire together）。每一個新的行為模式都在物理上重塑大腦迴路，為改變提供神經科學基礎。

【框架H】正念神經科學（Mindfulness Neuroscience，Jon Kabat-Zinn / Richie Davidson）
→ 適用：θ波低、High β高的分析。核心：正念訓練被神經影像學研究證實可顯著提升α波和θ波，降低過度活躍的β波，並增厚前額葉皮質，直接對應受測者的弱點。

【框架I】心流理論（Flow Theory，Mihaly Csikszentmihalyi）
→ 適用：親子活動設計、放鬆建議。核心：挑戰與技能的最佳匹配產生心流，腦波特徵為θ波上升、α波活躍，β波適度。

【框架J】溝通模式分析（Gottman Sound Relationship House + NVC非暴力溝通）
→ 適用：家庭溝通章節。核心：Gottman四騎士（批評、蔑視、防衛、冷戰）是關係崩壞的預測因子；NVC（觀察→感受→需要→請求）是重建安全溝通的工具。

═══ 撰寫核心原則 ═══

1. **直接命名問題**：第一段必須清楚、直接說出數據揭示的核心問題（例如：「數據直接告訴我們，這個家庭目前面臨的核心挑戰是：父親長期處於神經過載狀態，防衛機制已常態化，這在腦波上的具體表現是...」）。不迴避，不模糊，溫暖但直接。

2. **框架驅動分析**：在核心段落中，明確引入1～2個框架並解釋其與本節數據的對應關係（例如：「以多重迷走神經理論的視角來看，這組數據顯示...」）。

3. **數字即故事**：每個分析觀點必須引用至少2～3個具體腦波數字，說明「這個數字意味著什麼」，讓報告有不可辯駁的科學依據。

4. **溫暖不等於迴避**：承認困難與問題是對父母最深的尊重，不是傷害。語氣保持：「我看見了這個挑戰，正因為我看見，我才能給你真正有效的幫助」。

5. **可執行的結尾**：最後1～2個建議必須具體、可立即執行，並與框架對應（例如：「基於多重迷走神經理論，以下練習能直接激活腹側迷走神經...」）。

6. 讓父母讀完感受到：「我的問題被精準看見了、我理解了為什麼、我知道該怎麼做了、我有希望了」

7. 使用生動比喻讓抽象概念貼近生活，但比喻後必須回到數字與框架。
8. 【格式規定】輸出純文字段落，絕對不使用 **粗體**（** 符號）、# 標題或任何 Markdown 語法——報告將由系統排版，文字本身不需標記格式。
9. 【稱謂規定】稱呼有提供腦波數據的爸爸時一律用「名字+爸爸」，稱呼媽媽時一律用「名字+媽媽」，孩子直接稱其名字。絕對不使用「先生」、「女士」、「太太」等稱謂。
10. 【防止幻聽】對於未提供腦波數據的成員，只能使用其角色稱謂（爸爸/媽媽/孩子），絕對不得使用、假設或借用任何具體姓名。你只能使用本次報告中提示詞明確提供的姓名——禁止從訓練資料或任何上下文中引入未被授權的名字。

當某位成員數據缺失時：
• 段落開頭明確說明「由於此次未收集到XX的腦波數據，以下為基於家庭系統動力的預測分析」
• 根據其他成員的數據以及最相關的框架進行有邏輯的推測，說明推測依據"""


# ─────────────────────────────────────────────────────────────────────────────
# Helper utilities
# ─────────────────────────────────────────────────────────────────────────────
CH_NUMS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三"]
SEC_NUMS = ["一", "二", "三", "四"]


def _member_key_data(m: dict) -> str:
    """Return a compact string of the 2-3 most notable brainwave values for a member."""
    d = m.get("data") or {}
    metrics = d.get("metrics") or {}
    conc = d.get("concentration_pct", "?")
    relax = d.get("relaxation_pct", "?")
    # Pick the two most extreme (lowest) metrics as the focus areas
    metric_items = [(k, v) for k, v in metrics.items() if isinstance(v, (int, float))]
    metric_items.sort(key=lambda x: x[1])
    focus = "、".join(f"{k}{v}%" for k, v in metric_items[:2]) if metric_items else "無數據"
    return f"專注{conc}%、放鬆{relax}%；最需關注：{focus}"


def get_call_name(m: dict) -> str:
    """Return the correct address form: '名字+爸爸/媽媽' for parents, just name for children."""
    name = (m.get("name") or "").strip()
    role = m.get("role", "")
    role_zh = m.get("role_zh", "")
    if not name:
        return role_zh
    if role in ("dad", "father"):
        return f"{name}爸爸"
    elif role in ("mom", "mother"):
        return f"{name}媽媽"
    else:
        return name  # children: just use their name


def fix_honorifics(text: str, members: list) -> str:
    """Replace 'name+先生/女士/爸爸/媽媽' with the correct call names."""
    for m in members:
        name = (m.get("name") or "").strip()
        if not name:
            continue
        correct = get_call_name(m)
        # Replace wrong suffix patterns: name + 先生 / 女士 / 爸爸 / 媽媽
        wrong_suffixes = ["先生", "女士", "爸爸", "媽媽"]
        for suffix in wrong_suffixes:
            wrong = f"{name}{suffix}"
            if wrong != correct:
                text = text.replace(wrong, correct)
    return text


def ch_num_to_zh(num) -> str:
    if isinstance(num, int) and 1 <= num <= 13:
        return CH_NUMS[num - 1]
    return str(num)


def format_family_data(members: list) -> str:
    lines = []
    for m in members:
        role_zh = m.get("role_zh", m.get("role", ""))
        if m.get("present") and m.get("data"):
            # Only use the user-entered name when actual data is present
            name = m.get("name", role_zh)
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
            # Absent member: use ONLY the generic role label — never the entered name.
            # This prevents leaking names from previous sessions or form auto-fill.
            lines.append(f"\n【{role_zh}】（本次未提供腦波數據，以預測分析模式呈現，請以「{role_zh}」稱呼，勿使用任何具體姓名）")
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
        missing_note = (
            f"\n⚠️ 注意：{', '.join(missing)} 未提供腦波數據。"
            f"請以「預測分析」模式呈現，並在段首說明「由於本次未提供{'/'.join(missing)}的腦波數據，以下為預測分析」。"
            f"\n🚫 嚴禁：對未提供數據的成員使用任何具體姓名——只能使用其角色稱謂（{'/'.join(missing)}）。"
            f"不得假設或借用任何姓名。\n"
        )

    # Build call-name legend ONLY for present (data-provided) members
    call_names = []
    for m in members:
        if m.get("present"):
            call_names.append(f"  {m.get('role_zh', '')} → 稱為「{get_call_name(m)}」")
    absent_roles = [m.get("role_zh", "") for m in members if not m.get("present")]
    absent_note = (
        f"\n  未提供數據的成員（{', '.join(absent_roles)}）：一律只用角色稱謂，絕對不使用任何姓名。"
        if absent_roles else ""
    )
    call_name_note = (
        "【稱謂規定】（嚴格遵守，絕對不使用先生/女士/太太）\n"
        + "\n".join(call_names)
        + absent_note
    )

    # Special prompt for chapter 11 (未來風險防範與警訊應對)
    is_risk_chapter = (chapter.get("num") == 11)

    # Special prompt for the last chapter (六個月家庭重塑計畫, chapter 12)
    is_last_chapter = (chapter.get("num") == 12)

    if is_risk_chapter:
        prompt = f"""以下是這個家庭的完整腦波量測數據：
{family_data_str}
{missing_note}
{call_name_note}

請為以下章節撰寫深度分析報告：

📌 第十一章《{chapter['title']}》
📌 第{sec_zh}節《{section['title']}》

═══ 撰寫規格（嚴格遵守）═══

【字數】560～640字（繁體中文）

【本章核心定位】
這一章不是「列出警告清單」——而是讓家庭真正理解：
這個風險「為什麼會發生」、「大腦和神經系統怎麼解釋這個現象」、「如何在第一時間辨識並有效回應」。

【段落結構——必須依序呈現以下四層】

▌第一層：這個風險的根源在哪裡（約100字）
  — 直接說明這個特定風險（本節標題所指的那個人/那個模式）為何在壓力下容易「捲土重來」
  — 從腦波數據中指出最能預測此風險的關鍵指標（引用具體數字）
  — 不是預測災難，而是「看懂自己神經系統的舊習慣」

▌第二層：科學理論解釋復發機制（約180字）
  — 選取1～2個最相關的框架（依附理論、多重迷走神經、容忍之窗、情緒調節模型等）
  — 解釋：當壓力超過閾值時，大腦為什麼會自動切換回舊模式（神經塑性的雙面性）
  — 說明這個「退回舊軌道」不是意志力問題，而是神經系統的自動防禦邏輯
  — 引用至少2個具體腦波數值支撐論點

▌第三層：如何辨識「警訊訊號」（約130字）
  — 提供3～4個具體、可觀察的早期訊號（行為、語言、身體反應層面各一）
  — 讓家庭成員能在日常中「認出」自己或對方正在滑回舊模式
  — 語氣：不是批判，而是「我們一起學會看懂這個訊號」

▌第四層：即時解決方法（約150字）
  — 提供2～3個具體、可立即執行的回應策略
  — 必須根據對應的神經科學框架設計（如：激活腹側迷走神經的方法、擴展容忍之窗的練習）
  — 說明「在警訊出現的當下，第一步做什麼、第二步做什麼」
  — 至少一個策略是全家人可以一起執行的

【語氣】溫暖、務實、充滿掌握感——讓家庭感受到「我們知道怎麼應對了」
【格式禁止】絕對不使用 **粗體**（** 符號）、# 標題或任何 Markdown 格式——輸出純文字段落即可

請直接輸出報告正文（不需要重複標題，從第一個字直接開始）："""

    elif is_last_chapter:
        stage_map = {
            1: ("第1～6週",  "急性修復與降壓期",  "奠基：建立安全感與基礎調節"),
            2: ("第7～12週", "卸下鎧甲與解構期",  "深化：鬆動慣性模式與開放連結"),
            3: ("第13～18週","重新分配角色與重塑期","整合：角色重整與新互動習慣"),
            4: ("第19～24週","固化新模式與穩定期", "穩固：將改變內化為家庭文化"),
        }
        weeks_label, stage_theme, stage_goal = stage_map.get(section["num"], ("", "", ""))

        # Build member list with call names for present members
        present_members = [m for m in members if m.get("present")]
        member_lines = "\n".join(
            f"  {get_call_name(m)}（{m.get('role_zh','')}）：腦波關鍵數據 → {_member_key_data(m)}"
            for m in present_members
        )

        prompt = f"""以下是這個家庭的完整腦波量測數據：
{family_data_str}
{missing_note}
{call_name_note}

本次參與成員的關鍵數據摘要：
{member_lines}

請為以下章節撰寫「逐週可執行操練計畫」：

📌 第十二章《{chapter['title']}》
📌 第{sec_zh}節《{section['title']}》
本階段：{weeks_label}　主題：{stage_theme}　目標：{stage_goal}

═══ 撰寫規格（嚴格遵守）═══

【核心原則】這是一份「行動手冊」，不是心理分析——每一行都必須是家庭成員「今天/這週可以立刻執行」的具體行為。

【輸出格式——嚴格照此結構，逐週列出全部6週】

第一週　[本週主題，8字以內]
◆ [成員稱謂]：[具體動作] [頻率/時長]，[觀察目標]
◆ [成員稱謂]：[具體動作] [頻率/時長]，[觀察目標]
◆ [成員稱謂]：[具體動作] [頻率/時長]，[觀察目標]
家庭共同任務：[全家一起做的事，1句話]

第二週　[本週主題]
◆ … (同上格式)

（依序完成第一週至第六週，共6個單元）

【動作規格】每個◆任務必須包含：
  做什麼（具體行為，如「腹式呼吸4秒吸6秒呼」，不是「練習放鬆」）
  多久/幾次（如「每天睡前5分鐘」「每週三次各10分鐘」）
  可觀察的微目標（如「本週能做到3次不中途中斷」）

【數據連結】每個人的任務必須對應其最需改善的腦波指標（從上方數據摘要中選）

【遞進邏輯】
  第1～2週：建立最小可行習慣（門檻極低，確保做得到）
  第3～4週：加深強度或加入互動元素
  第5～6週：家人開始互相觀察並給予回饋

【字數】650～780字（繁體中文，6週完整呈現，字數可略超過一般節次以確保完整性）
【格式禁止】絕對不使用 **粗體**（** 符號）、# 標題或任何 Markdown 格式

請直接輸出計畫內文（不重複章節標題，從「第一週」開始）："""
    else:
        prompt = f"""以下是這個家庭的完整腦波量測數據：
{family_data_str}
{missing_note}
{call_name_note}
請為以下章節撰寫深度心理分析報告：

📌 第{ch_zh}章《{chapter['title']}》
📌 第{sec_zh}節《{section['title']}》

═══ 撰寫規格（嚴格遵守）═══

【字數】560～640字（繁體中文）

【段落結構——必須依序呈現以下四層】

▌第一層：直接命名核心問題（約100字）
  — 開門見山，直接說出數據揭示的核心問題或挑戰是什麼
  — 使用具體數字支撐（例如：「您的內在安定僅有XX%，這直接揭示了一個需要正視的問題：...」）
  — 不迴避、不模糊，溫暖但直接

▌第二層：科學框架深度解析（約200字）
  — 從「框架庫」中選出1～2個最相關的框架，在文中明確說出框架名稱
  — 以框架視角解釋「為什麼這個問題會發生」「這個模式的神經科學機制是什麼」
  — 引用2～3個具體腦波數字，說明它們與框架的對應關係
  — 例如：「以多重迷走神經理論（Polyvagal Theory）的視角來看，XX%的高頻Beta加上XX%的低α，清楚顯示您的神經系統長期鎖定在交感神經的戰或逃模式中...」

▌第三層：問題影響的真實面貌（約150字）
  — 具體說明這個問題如何在家庭日常生活中呈現，讓父母能認出自己的真實狀況
  — 使用生動比喻連結抽象概念，但比喻後必須回到數據
  — 展現深刻的理解與同理，讓父母感受到「被精準看見」

▌第四層：具體行動建議（約120字）
  — 提供1～2個具體、可立即執行的建議
  — 建議必須與所選框架直接對應（例如：「根據多重迷走神經理論，以下方法能直接激活腹側迷走神經...」）
  — 以「建議您...」或「這週可以嘗試：」開頭
  — 建議要具體到「每天幾分鐘、做什麼動作、預期效果是什麼」

【語氣】溫暖、直接、充滿力量——如同一位對你坦誠相告的高階心理醫生
【禁止】迴避問題、用模糊詞語掩蓋問題、只說好聽的話
【格式禁止】絕對不使用 **粗體**（即 ** 符號）、不使用 # 標題符號、不使用 Markdown 格式——輸出純文字段落即可
【要求】讓父母讀完感受到：「問題被精準看見了」「我理解了為什麼」「我知道具體怎麼做了」「我有希望了」

請直接輸出報告正文（不需要重複標題，從第一個字直接開始）："""

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
            text = text.strip()
            # Post-process: ensure correct honorifics (replace 先生/女士 with 爸爸/媽媽)
            text = fix_honorifics(text, members)
            return text
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                return f"（此節內容暫時無法生成，請稍後重試。錯誤訊息：{e}）"


def generate_section_image(section_title: str, chapter_title: str, text_preview: str, job_id: str, key: str):
    """Generate illustration via Gemini image model and save locally. Returns local URL path or None."""
    prompt = f"""Create a warm, clear editorial illustration for a printed East Asian family psychology brainwave report.

SECTION: "{section_title}"  |  CHAPTER: "{chapter_title}"

WHAT THE TEXT IS ABOUT (read to understand what to draw):
---
{text_preview[:300]}
---

YOUR GOAL: Draw an illustration that lets the reader INSTANTLY understand the main message of this section — no guessing, no abstract puzzles. The image should feel like a high-quality magazine infographic meets warm Asian editorial art.

HOW TO DECIDE WHAT TO DRAW:
• Read the text above carefully.
• Identify the SINGLE clearest concept (e.g. "parents and child are emotionally disconnected", "family members are draining each other's energy", "a nervous system stuck in fight-or-flight", "two parents resonating through brainwaves").
• Visualise it as a CONCRETE scene or diagram. Use these approaches in order of preference:
  1. Warm family silhouette scene showing the exact dynamic (e.g. parent and child sitting back-to-back with visible tension lines, or parents holding hands around a child in a warm glow)
  2. Simple symbolic diagram with clear labels (e.g. a gauge showing "energy level 31%", two interlinked gears labelled "媽媽" and "爸爸", a brain with a narrow window labelled "容忍之窗")
  3. A clear metaphor object with short label (e.g. a nearly-empty lantern labelled "氣血飽滿 31%", two magnets facing the same pole with label "防禦機制")

STYLE:
• Warm editorial illustration — think Nikkei magazine or Harvard Business Review infographic, but with Asian warmth
• Soft watercolour + clean line art hybrid: bold but gentle
• Colour palette: sage green (#5B7B5B), warm gold (#C4923A), soft cream (#FAF7F2), misty teal
• Characters: SIMPLE SILHOUETTES (no realistic faces) — parent/child figures clearly distinguishable by size
• Text in image: SHORT Chinese labels (2–6 characters) ARE ALLOWED and encouraged if they make the concept clearer

COMPOSITION:
• Horizontal 16:9 format
• One clear focal element in the centre-right
• Supporting elements on the left
• Clean background — gradient from warm cream to soft teal, no busy patterns

QUALITY: Professional enough for a premium printed psychological report — clear, warm, meaningful at a glance."""

    for attempt in range(3):
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
                    if isinstance(raw, bytes):
                        img_path.write_bytes(raw)
                    else:
                        img_path.write_bytes(base64.b64decode(raw))
                    print(f"[IMAGE OK] key={key} attempt={attempt+1}", flush=True)
                    return f"/static/report_images/{job_id}/{key}.png"
            print(f"[IMAGE EMPTY] key={key} attempt={attempt+1} — no inline_data in response", flush=True)
            return None
        except Exception as e:
            err = str(e)
            print(f"[IMAGE ERR] key={key} attempt={attempt+1} error={err[:120]}", flush=True)
            if attempt < 2:
                wait = 8 * (attempt + 1)   # 8s, 16s between retries
                print(f"[IMAGE RETRY] waiting {wait}s…", flush=True)
                time.sleep(wait)
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
                    section["title"], chapter["title"], text[:300], job_id, key
                )
                # Brief pause after image gen to respect API rate limits
                if image_path:
                    time.sleep(3)

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
    cover_exists    = (Path('static') / '親子互動報告_封面.png').exists()
    backcover_exists = (Path('static') / '親子互動報告_封底.png').exists()
    return render_template(
        "index.html",
        api_key_set=key_is_set(),
        cover_url    = '/static/親子互動報告_封面.png'   if cover_exists     else None,
        backcover_url = '/static/親子互動報告_封底.png' if backcover_exists else None,
    )


@app.route("/upload-cover", methods=["POST"])
def upload_cover():
    """Replace cover or back-cover image (base64 encoded)."""
    data = request.get_json(force=True, silent=True) or {}
    kind     = data.get("kind")       # "cover" | "backcover"
    img_b64  = data.get("image_b64")
    if kind not in ("cover", "backcover") or not img_b64:
        return jsonify({"ok": False, "error": "Missing kind or image_b64"}), 400
    filename = "親子互動報告_封面.png" if kind == "cover" else "親子互動報告_封底.png"
    try:
        img_bytes = base64.b64decode(img_b64)
        dst = Path("static") / filename
        with open(dst, "wb") as f:
            f.write(img_bytes)
        return jsonify({"ok": True, "url": f"/static/{filename}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/extract", methods=["POST"])
def extract():
    """Use Gemini vision to extract brainwave data from an uploaded image."""
    # Use force=True + silent=True to handle any Content-Type edge cases
    data = request.get_json(force=True, silent=True)
    if not data:
        size = request.content_length or 0
        print(f"[EXTRACT 400] JSON parse failed. Content-Length={size}, Content-Type={request.content_type}", flush=True)
        msg = "圖片檔案過大，請先壓縮後再上傳（建議小於 8 MB）" if size > 18_000_000 else "請求格式錯誤，無法解析 JSON body"
        return jsonify({"success": False, "error": "bad_request", "message": msg}), 400

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

    def _call_extract(use_json_mime: bool):
        """Call Gemini once; returns parsed dict or raises."""
        image_bytes = base64.b64decode(image_b64)
        cfg = types.GenerateContentConfig(max_output_tokens=2048)
        if use_json_mime:
            cfg = types.GenerateContentConfig(
                max_output_tokens=2048,
                response_mime_type="application/json",
            )
        resp = get_client().models.generate_content(
            model=EXTRACT_MODEL,
            contents=[
                types.Part(inline_data=types.Blob(mime_type=image_type, data=image_bytes)),
                extraction_prompt,
            ],
            config=cfg,
        )
        raw = _get_response_text(resp)
        if not raw:
            fr = resp.candidates[0].finish_reason if resp.candidates else "unknown"
            raise ValueError(f"Gemini 回傳空內容（finish_reason={fr}）")
        return raw

    def _parse_json(raw: str) -> dict:
        """Try multiple strategies to parse JSON from raw text."""
        # Strategy 1: strip code fences then parse directly
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1]
            clean = clean.rsplit("```", 1)[0].strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            pass
        # Strategy 2: regex — grab outermost { ... }
        m = re.search(r'\{[\s\S]*\}', clean)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        raise json.JSONDecodeError("No valid JSON found", clean, 0)

    try:
        raw_text = None
        try:
            # First attempt: ask for JSON MIME type (most reliable)
            raw_text = _call_extract(use_json_mime=True)
            result = _parse_json(raw_text)
        except json.JSONDecodeError:
            print(f"[EXTRACT] JSON mime parse failed, retrying without mime hint. raw={str(raw_text)[:200]}", flush=True)
            time.sleep(2)
            # Second attempt: plain text, rely on prompt instructions
            raw_text = _call_extract(use_json_mime=False)
            result = _parse_json(raw_text)

        print(f"[EXTRACT OK] keys={list(result.keys())}", flush=True)
        return jsonify({"success": True, "data": result})

    except json.JSONDecodeError as e:
        raw = raw_text or "N/A"
        print(f"[EXTRACT JSON ERROR] {e} | raw: {str(raw)[:300]}", flush=True)
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
    has_cover = (Path('static') / '親子互動報告_封面.png').exists()
    has_backcover = (Path('static') / '親子互動報告_封底.png').exists()
    cover_url    = '/static/親子互動報告_封面.png'   if has_cover     else None
    backcover_url = '/static/親子互動報告_封底.png' if has_backcover else None

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
            cover_url=cover_url,
            backcover_url=backcover_url,
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
            cover_url=cover_url,
            backcover_url=backcover_url,
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
