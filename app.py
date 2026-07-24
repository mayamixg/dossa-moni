import json
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request

# [설정] 디스코드 웹훅 URL (Render 환경 변수 등록 권장)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "여기에_디스코드_웹훅_주소_입력")
CONFIG_FILE = "moni_config.json"

app = Flask(__name__)
KST = timezone(timedelta(hours=9))

def get_now_kst():
    return datetime.now(KST).strftime("%H:%M:%S")

seen_products = set()
running_channels = [False] * 5
threads = [None] * 5
next_run_times = [0] * 5
last_run_times = ["-"] * 5
recent_matches = []

# 해외 IP 차단 방지 및 한국 브라우저 위장용 HTTP 헤더
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://corearoadbike.com/",
    "Connection": "close"
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return [
        {"name": "도싸 중고장터", "url": "https://corearoadbike.com/board/board.php?t_id=Menu31Top6", "kw": "e1, d1, 크랭크, 레드, 크랭크암"},
        {"name": "도싸 업체장터", "url": "https://corearoadbike.com/board/board.php?t_id=Menu31Top1", "kw": "휠셋, 가민, 보라, 시마노"},
        {"name": "감시 채널 3", "url": "", "kw": ""},
        {"name": "감시 채널 4", "url": "", "kw": ""},
        {"name": "감시 채널 5", "url": "", "kw": ""}
    ]

def save_config_data(data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"설정 저장 실패: {e}")

def send_discord_message(title, link, label):
    if not DISCORD_WEBHOOK_URL or "여기에_" in DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [{
            "title": f"🚨 [신규 발견] - {label} 🚨",
            "description": f"**위치:** {label}\n**제목:** {title}\n\n[👉 게시글 바로가기]({link})",
            "color": 16738654
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, headers=HEADERS, timeout=5)
    except Exception as e:
        print(f"디스코드 연동 오류: {e}")

def check_market(url, keywords, label, is_initial_run):
    global recent_matches
    try:
        # 연결 타임아웃 10초, 데이터 수신 타임아웃 15초 지정
        res = requests.get(url, headers=HEADERS, timeout=(10, 15))
        if res.status_code != 200:
            print(f"[{get_now_kst()}] [{label}] 요청 실패 (상태 코드: {res.status_code})")
            return

        # 도싸 한글 인코딩(EUC-KR) 적용
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, "html.parser")
        
        links = soup.find_all("a")
        found_count = 0

        for a in links:
            href = a.get("href", "")
            title = a.get_text(strip=True)

            # 도싸 게시글 링크 식별 (board.php 및 no= 파라미터 포함 조건)
            if "board.php" not in href or "no=" not in href or not title or len(title) < 2:
                continue

            link = href if href.startswith("http") else f"https://corearoadbike.com/board/{href.lstrip('./')}"
            numbers = re.findall(r'no=(\d+)', link)
            product_id = numbers[0] if numbers else link

            title_lower = title.lower()
            is_match = any(kw.strip().lower() in title_lower for kw in keywords if kw.strip())

            if is_match:
                found_count += 1
                if product_id not in seen_products:
                    seen_products.add(product_id)
                    now_str = get_now_kst()
                    
                    if not any(m["link"] == link for m in recent_matches):
                        recent_matches.insert(0, {
                            "time": now_str,
                            "channel": label,
                            "title": title,
                            "link": link
                        })
                        if len(recent_matches) > 50:
                            recent_matches.pop()

                    # 최초 실행(기존 매물) 시 디스코드 알림 제외, 가동 중 새 매물 포착시에만 발송
                    if not is_initial_run:
                        send_discord_message(title, link, label)

        print(f"[{get_now_kst()}] [{label}] 탐색 완료 - 매칭된 매물 수: {found_count}개")

    except requests.exceptions.Timeout:
        print(f"[{get_now_kst()}] [{label}] 도싸 서버 응답 지연(Timeout) - 다음 주기에 재시도합니다.")
    except Exception as e:
        print(f"[{get_now_kst()}] [{label}] 크롤링 오류 발생: {e}")

def channel_loop(idx):
    global next_run_times, last_run_times
    first_run = True

    while running_channels[idx]:
        config = load_config()
        ch = config[idx]
        last_run_times[idx] = get_now_kst()

        if ch["url"] and ch["kw"]:
            keywords = [k.strip() for k in ch["kw"].split(",") if k.strip()]
            check_market(ch["url"], keywords, ch["name"] or f"채널 {idx+1}", is_initial_run=first_run)

        first_run = False
        sleep_time = random.randint(180, 270)  # 3분~4분 30초 랜덤 대기
        next_run_times[idx] = time.time() + sleep_time

        for _ in range(sleep_time):
            if not running_channels[idx]:
                next_run_times[idx] = 0
                return
            time.sleep(1)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>매물 모니터 제어판</title>
    <style>
        body { font-family: 'Malgun Gothic', sans-serif; background: #f8f9fa; padding: 15px; }
        .container { max-width: 900px; margin: 0 auto; background: white; padding: 20px; border-radius: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); }
        h2, h3 { text-align: center; color: #1e272e; margin-bottom: 20px; }
        .channel-card { border: 1px solid #dcdde1; border-radius: 8px; padding: 15px; margin-bottom: 15px; background: #fff; }
        .row { display: flex; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; align-items: center; }
        input { padding: 8px 12px; border: 1px solid #ccc; border-radius: 5px; font-size: 14px; }
        .inp-name { width: 110px; }
        .inp-url { flex: 1; min-width: 180px; }
        .inp-kw { flex: 2; min-width: 180px; }
        .btn { padding: 8px 16px; border: none; border-radius: 5px; color: white; font-weight: bold; cursor: pointer; }
        .btn-start { background: #4cd137; }
        .btn-stop { background: #e84118; }
        .btn-save { background: #0097e6; width: 100%; margin-top: 10px; padding: 12px; font-size: 16px; }
        .status-badge { font-weight: bold; padding: 4px 8px; border-radius: 4px; font-size: 13px; }
        .st-on { background: #e1f5fe; color: #0288d1; }
        .st-off { background: #ffebee; color: #c62828; }
        .info-text { font-size: 12px; color: #666; margin-top: 4px; }
        
        .log-section { margin-top: 30px; border-top: 2px dashed #dcdde1; padding-top: 20px; }
        .log-list { list-style: none; padding: 0; margin: 0; max-height: 350px; overflow-y: auto; }
        .log-item { background: #f1f2f6; border-left: 4px solid #0097e6; padding: 10px 12px; margin-bottom: 8px; border-radius: 4px; font-size: 13px; }
        .log-time { font-size: 11px; color: #718093; margin-right: 8px; }
        .log-channel { font-weight: bold; color: #2f3542; margin-right: 8px; }
        .log-title { color: #27ae60; text-decoration: none; font-weight: bold; }
        .log-title:hover { text-decoration: underline; }
        .empty-log { text-align: center; color: #888; font-size: 13px; padding: 20px 0; }
    </style>
</head>
<body>
<div class="container">
    <h2>📡 매물 모니터 제어판</h2>
    <form id="configForm">
        {% for i in range(5) %}
        <div class="channel-card">
            <div class="row">
                <span class="status-badge st-off" id="status-{{i}}">🔴 중지됨</span>
                <input type="text" class="inp-name" name="name_{{i}}" value="{{ config[i].name }}" placeholder="채널명">
                <input type="text" class="inp-url" name="url_{{i}}" value="{{ config[i].url }}" placeholder="감시 URL">
            </div>
            <div class="row">
                <input type="text" class="inp-kw" name="kw_{{i}}" value="{{ config[i].kw }}" placeholder="키워드 (쉼표 분리)">
                <button type="button" class="btn btn-start" id="btn-{{i}}" onclick="toggleChannel({{i}})">▶ 시작</button>
            </div>
            <div class="info-text" id="info-{{i}}">최근 탐색: - | 다음 탐색까지: -</div>
        </div>
        {% endfor %}
        <button type="button" class="btn btn-save" onclick="saveConfig()">💾 설정 자동 저장</button>
    </form>

    <div class="log-section">
        <h3>🔔 포착된 매물 목록 (실시간)</h3>
        <ul class="log-list" id="matchLog">
            <li class="empty-log">아직 감지된 매물이 없습니다.</li>
        </ul>
    </div>
</div>

<script>
    function updateStatus() {
        fetch('/api/status')
            .then(res => res.json())
            .then(data => {
                data.channels.forEach((ch, i) => {
                    const btn = document.getElementById(`btn-${i}`);
                    const status = document.getElementById(`status-${i}`);
                    const info = document.getElementById(`info-${i}`);
                    
                    if (ch.is_running) {
                        btn.innerText = "🛑 중지";
                        btn.className = "btn btn-stop";
                        status.innerText = "🟢 가동중";
                        status.className = "status-badge st-on";
                        
                        let timerText = ch.remaining_sec > 0 ? `${ch.remaining_sec}초 후 탐색` : '탐색 진행 중...';
                        info.innerText = `최근 탐색: ${ch.last_run} | 다음 탐색까지: ${timerText}`;
                    } else {
                        btn.innerText = "▶ 시작";
                        btn.className = "btn btn-start";
                        status.innerText = "🔴 중지됨";
                        status.className = "status-badge st-off";
                        info.innerText = `최근 탐색: ${ch.last_run} | 다음 탐색까지: -`;
                    }
                });

                const matchLog = document.getElementById('matchLog');
                if (data.matches.length > 0) {
                    matchLog.innerHTML = data.matches.map(m => `
                        <li class="log-item">
                            <span class="log-time">[${m.time}]</span>
                            <span class="log-channel">[${m.channel}]</span>
                            <a href="${m.link}" target="_blank" class="log-title">${m.title}</a>
                        </li>
                    `).join('');
                } else {
                    matchLog.innerHTML = '<li class="empty-log">아직 감지된 매물이 없습니다.</li>';
                }
            });
    }

    function toggleChannel(idx) {
        saveConfig(() => {
            fetch(`/api/toggle/${idx}`, { method: 'POST' })
                .then(res => res.json())
                .then(() => updateStatus());
        });
    }

    function saveConfig(callback) {
        const formData = new FormData(document.getElementById('configForm'));
        fetch('/api/save', { method: 'POST', body: formData })
            .then(res => res.json())
            .then(data => {
                if (callback) callback();
            });
    }

    setInterval(updateStatus, 3000);
    updateStatus();
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, config=load_config())

@app.route("/api/status")
def api_status():
    now = time.time()
    status_list = []
    for i in range(5):
        rem = int(next_run_times[i] - now) if running_channels[i] else 0
        status_list.append({
            "is_running": running_channels[i],
            "last_run": last_run_times[i],
            "remaining_sec": max(0, rem)
        })
    return jsonify({"channels": status_list, "matches": recent_matches})

@app.route("/api/save", methods=["POST"])
def api_save():
    new_config = []
    for i in range(5):
        new_config.append({
            "name": request.form.get(f"name_{i}", "").strip(),
            "url": request.form.get(f"url_{i}", "").strip(),
            "kw": request.form.get(f"kw_{i}", "").strip()
        })
    save_config_data(new_config)
    return jsonify({"status": "success"})

@app.route("/api/toggle/<int:idx>", methods=["POST"])
def api_toggle(idx):
    global running_channels, threads
    if not running_channels[idx]:
        running_channels[idx] = True
        t = threading.Thread(target=channel_loop, args=(idx,), daemon=True)
        threads[idx] = t
        t.start()
    else:
        running_channels[idx] = False
    return jsonify({"status": "success", "is_running": running_channels[idx]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
