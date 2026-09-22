import io
import os
import shutil
import datetime
from urllib.parse import quote
import requests
import streamlit as st
import chess
import chess.engine
import chess.pgn
import duckdb
import pandas as pd

# --- CONFIGURATION & DYNAMIC PATH RESOLUTION ---
def find_stockfish():
    # 1. Standard Debian/Ubuntu Linux apt paths (Streamlit Cloud)
    cloud_paths = [
        "/usr/games/stockfish",
        "/usr/bin/stockfish",
        "/usr/local/bin/stockfish"
    ]
    for path in cloud_paths:
        if os.path.exists(path):
            return path

    # 2. General environment PATH
    which_path = shutil.which("stockfish")
    if which_path:
        return which_path

    # 3. Local Windows fallback
    windows_path = r"C:/Users/danho/Downloads/stockfish/stockfish-windows-x86-64-universal.exe"
    if os.path.exists(windows_path):
        return windows_path

    return None

STOCKFISH_PATH = find_stockfish()
MAX_MOVE_NUMBER = 15
ENGINE_LIMIT = chess.engine.Limit(depth=10)

st.set_page_config(page_title="Chess Telemetry & Leak Scanner", layout="wide")

if not STOCKFISH_PATH:
    st.error(
        "Stockfish engine binary could not be found. "
        "If running on Streamlit Cloud, make sure `packages.txt` exists in your repository root containing `stockfish`."
    )
    st.stop()

# --- HELPER FUNCTIONS ---
def get_my_eval(info, my_color):
    score = info["score"].white() if my_color == chess.WHITE else info["score"].black()
    if score.is_mate():
        return 100.0 if score.mate() > 0 else -100.0
    return (score.score() or 0) / 100.0

def fetch_chesscom_games(username, max_games=20):
    now = datetime.datetime.now()
    year = now.strftime("%Y")
    month = now.strftime("%m")
    
    url = f"https://api.chess.com/pub/player/{username.lower()}/games/{year}/{month}"
    headers = {"User-Agent": f"ChessLeakScanner/2.0 ({username}@portfolio.com)"}
    
    resp = requests.get(url, headers=headers)
    if resp.status_code == 404:
        st.error(f"User '{username}' not found on Chess.com.")
        return []
    elif resp.status_code != 200:
        st.error(f"API request failed with status code {resp.status_code}.")
        return []

    data = resp.json()
    games_raw = data.get("games", [])
    return games_raw[-max_games:]

def analyze_games(games_list, target_username, min_drop, max_drop):
    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    engine.configure({"Threads": 2, "Hash": 128})

    leaks = []
    progress_bar = st.progress(0, text="Evaluating telemetry...")
    total = len(games_list)

    for idx, g in enumerate(games_list):
        pgn_text = g.get("pgn")
        if not pgn_text:
            continue

        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if not game:
            continue

        white_player = game.headers.get("White", "Unknown")
        black_player = game.headers.get("Black", "Unknown")
        target = target_username.lower()

        if white_player.lower() == target:
            my_color = chess.WHITE
            color_str = "White"
            is_flip = "false"
        elif black_player.lower() == target:
            my_color = chess.BLACK
            color_str = "Black"
            is_flip = "true"
        else:
            continue

        board = game.board()
        moves = list(game.mainline_moves())
        opening_name = game.headers.get("Opening", "Standard Opening")
        date_played = game.headers.get("Date", "Unknown")

        # Clean PGN string for URL transmission
        clean_pgn = pgn_text.replace("\r\n", " ").replace("\n", " ").strip()
        encoded_pgn = quote(clean_pgn)

        info_current = engine.analyse(board, ENGINE_LIMIT)
        prev_eval = get_my_eval(info_current, my_color)

        for ply_index, move in enumerate(moves):
            move_num = (ply_index // 2) + 1
            if move_num > MAX_MOVE_NUMBER:
                break

            if board.turn == my_color:
                # Capture exact board state before the blunder
                fen_before = board.fen()
                encoded_fen = quote(fen_before)

                # 1. Chess.com link (exact FEN board state flipped to player perspective)
                chesscom_url = f"https://www.chess.com/analysis?fen={encoded_fen}&flip={is_flip}"
                
                # 2. Lichess link (loads full game history and parks right at the blunder ply)
                target_ply = ply_index
                lichess_url = f"https://lichess.org/analysis/pgn/{encoded_pgn}#{target_ply}"

                pv = info_current.get("pv", [])
                best_move = board.san(pv[0]) if pv else "N/A"
                played_move = board.san(move)

                board.push(move)
                info_next = engine.analyse(board, ENGINE_LIMIT)
                eval_after = get_my_eval(info_next, my_color)

                eval_drop = round(prev_eval - eval_after, 2)

                # Filter within user-defined tolerance range
                if min_drop <= eval_drop <= max_drop:
                    if eval_drop >= 1.50:
                        severity = "Blunder"
                        badge_color = "#FF4B4B"  # Red
                    elif eval_drop >= 0.80:
                        severity = "Mistake"
                        badge_color = "#FFA500"  # Orange
                    else:
                        severity = "Inaccuracy"
                        badge_color = "#F0D000"  # Yellow

                    leaks.append({
                        "game_id": f"Game {idx + 1}: {white_player} vs {black_player} ({date_played})",
                        "date": date_played,
                        "color": color_str,
                        "opening": opening_name,
                        "move_number": move_num,
                        "played_move": played_move,
                        "engine_best": best_move,
                        "eval_drop": eval_drop,
                        "severity": severity,
                        "badge_color": badge_color,
                        "chesscom_url": chesscom_url,
                        "lichess_url": lichess_url
                    })

                prev_eval = eval_after
                info_current = info_next
            else:
                board.push(move)
                info_current = engine.analyse(board, ENGINE_LIMIT)
                prev_eval = get_my_eval(info_current, my_color)

        progress_bar.progress((idx + 1) / total, text=f"Audited game {idx + 1} of {total}")

    engine.quit()
    return leaks

# --- STREAMLIT UI ---
st.title("♟️ Player Telemetry & Leak Detection Engine")
st.caption("Heuristic detection pipeline isolating opening inaccuracies and blunders directly from game logs.")

with st.sidebar:
    st.header("Search Parameters")
    username_input = st.text_input("Chess.com Username", value="danhonda")
    game_limit = st.slider("Games to Analyze", min_value=5, max_value=50, value=15, step=5)
    
    st.markdown("---")
    st.subheader("Tolerance Calibration")
    eval_range = st.slider(
        "Eval Drop Range (Pawns Lost)",
        min_value=0.20,
        max_value=3.50,
        value=(0.30, 1.20),
        step=0.10,
        help="Filter moves by how much advantage was dropped."
    )
    min_drop, max_drop = eval_range

    run_btn = st.button("Run Anomaly Scan", type="primary")

if run_btn and username_input:
    with st.spinner(f"Pulling recent telemetry for {username_input}..."):
        recent_games = fetch_chesscom_games(username_input, max_games=game_limit)

    if recent_games:
        st.info(f"Loaded {len(recent_games)} matches. Running engine audit for drop range **{min_drop:.2f} to {max_drop:.2f} pawns**...")
        leaks_data = analyze_games(recent_games, username_input, min_drop, max_drop)

        if leaks_data:
            leaks_df = pd.DataFrame(leaks_data)
            con = duckdb.connect()
            con.register("leaks_df", leaks_df)

            # High-level KPIs
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Games Evaluated", len(recent_games))
            c2.metric("Flagged Inaccuracies", len(leaks_data))
            avg_loss = con.execute("SELECT ROUND(AVG(eval_drop), 2) FROM leaks_df").fetchone()[0]
            c3.metric("Avg Drop / Leak", f"-{avg_loss} pawns")
            affected_games = con.execute("SELECT COUNT(DISTINCT game_id) FROM leaks_df").fetchone()[0]
            c4.metric("Games with Leaks", affected_games)

            st.markdown("---")
            st.subheader("Audited Matches & Inaccuracies")

            # Grouping by Game
            grouped = leaks_df.groupby("game_id")
            for game_title, group in grouped:
                with st.expander(f"📌 **{game_title}** — {len(group)} flagged move(s)", expanded=True):
                    for _, row in group.iterrows():
                        col_tag, col_move, col_rec, col_drop, col_c, col_l = st.columns([1.2, 1.2, 1.2, 1.2, 2.2, 2.2])
                        
                        # Severity badge
                        col_tag.markdown(
                            f"<span style='background-color:{row['badge_color']}; color:black; font-weight:bold; padding:3px 8px; border-radius:4px;'>{row['severity']}</span>",
                            unsafe_allow_html=True
                        )
                        col_move.markdown(f"**Move {row['move_number']}:** `{row['played_move']}`")
                        col_rec.markdown(f"**Best:** `{row['engine_best']}`")
                        col_drop.markdown(f"**Loss:** `-{row['eval_drop']}`")
                        col_c.markdown(f"[♟️ Chess.com Board]({row['chesscom_url']})")
                        col_l.markdown(f"[📖 Full Replay @ Move]({row['lichess_url']})")

            st.markdown("---")
            st.subheader("Most Repeated Systemic Leaks (DuckDB Aggregation)")
            summary_query = """
            SELECT 
                color,
                opening,
                move_number AS move_num,
                played_move,
                engine_best,
                COUNT(*) AS times_repeated,
                ROUND(AVG(eval_drop), 2) AS avg_eval_lost
            FROM leaks_df
            GROUP BY color, opening, move_number, played_move, engine_best
            ORDER BY times_repeated DESC, avg_eval_lost DESC
            LIMIT 10
            """
            st.dataframe(con.execute(summary_query).df(), use_container_width=True, hide_index=True)

        else:
            st.success(f"No leaks found within the range of {min_drop:.2f} to {max_drop:.2f} pawns.")
