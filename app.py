import io
import os
import shutil
import datetime
import base64
from urllib.parse import quote
import requests
import streamlit as st
import chess
import chess.engine
import chess.pgn
import chess.svg
import duckdb
import pandas as pd

# --- CONFIGURATION & PATH RESOLUTION ---
def find_stockfish():
    cloud_paths = [
        "/usr/games/stockfish",
        "/usr/bin/stockfish",
        "/usr/local/bin/stockfish"
    ]
    for path in cloud_paths:
        if os.path.exists(path):
            return path

    which_path = shutil.which("stockfish")
    if which_path:
        return which_path

    windows_path = r"C:/Users/danho/Downloads/stockfish/stockfish-windows-x86-64-universal.exe"
    if os.path.exists(windows_path):
        return windows_path

    return None

STOCKFISH_PATH = find_stockfish()
MAX_MOVE_NUMBER = 15
ENGINE_LIMIT = chess.engine.Limit(depth=10)

st.set_page_config(page_title="Chess Telemetry & Opening Leak Scanner", layout="wide")

if not STOCKFISH_PATH:
    st.error(
        "Stockfish engine binary could not be found. "
        "If running on Streamlit Cloud, make sure packages.txt exists containing stockfish."
    )
    st.stop()

# --- CSS FOR TIGHT PADDING & LARGE READABLE CARDS ---
st.markdown("""
<style>
div[data-testid="stExpander"] div[data-testid="stColumn"] {
    padding: 2px 5px !important;
}
div[data-testid="stExpander"] div[data-testid="stVerticalBlockBorderWrapper"] {
    padding: 8px 12px !important;
    margin-bottom: 6px !important;
}
.large-move-text {
    font-size: 19px !important;
    font-weight: 800 !important;
    line-height: 1.35 !important;
    letter-spacing: -0.3px;
    margin-bottom: 4px;
}
.large-stat-text {
    font-size: 14px !important;
    color: #9ca3af !important;
    margin-bottom: 8px;
}
</style>
""", unsafe_allow_html=True)

# --- HELPER FUNCTIONS ---
def get_my_eval(info, my_color):
    score = info["score"].white() if my_color == chess.WHITE else info["score"].black()
    if score.is_mate():
        return 100.0 if score.mate() > 0 else -100.0
    return (score.score() or 0) / 100.0

def get_opening_signature_and_fen(game, plies=4):
    temp_board = game.board()
    moves = list(game.mainline_moves())[:plies]
    
    move1_parts = []
    move2_parts = []
    
    for idx, move in enumerate(moves):
        san = temp_board.san(move)
        temp_board.push(move)
        if idx == 0:
            move1_parts.append(f"1. {san}")
        elif idx == 1:
            move1_parts.append(san)
        elif idx == 2:
            move2_parts.append(f"2. {san}")
        elif idx == 3:
            move2_parts.append(san)

    line1 = " ".join(move1_parts) if move1_parts else "1. ..."
    line2 = " ".join(move2_parts) if move2_parts else "2. ..."
    flat_sig = f"{line1} {line2}".strip()
    
    return line1, line2, flat_sig, temp_board.fen()

def render_svg_board(fen, player_color, size=130):
    b = chess.Board(fen)
    orientation = chess.WHITE if player_color == "White" else chess.BLACK
    svg_data = chess.svg.board(board=b, orientation=orientation, size=size, coordinates=False)
    b64 = base64.b64encode(svg_data.encode("utf-8")).decode("utf-8")
    return f'<img src="data:image/svg+xml;base64,{b64}" width="{size}" height="{size}" style="border-radius:6px; border:1px solid #555; display:block;" />'

def fetch_chesscom_games(username, max_games=30):
    now = datetime.datetime.now()
    year = now.strftime("%Y")
    month = now.strftime("%m")
    
    url = f"https://api.chess.com/pub/player/{username.lower()}/games/{year}/{month}"
    headers = {"User-Agent": f"ChessLeakScanner/6.0 ({username}@portfolio.com)"}
    
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

def index_games_metadata(games_list, target_username, chosen_color):
    indexed = []
    target = target_username.lower()

    for idx, g in enumerate(games_list):
        pgn_text = g.get("pgn")
        if not pgn_text:
            continue

        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if not game:
            continue

        white_player = game.headers.get("White", "Unknown")
        black_player = game.headers.get("Black", "Unknown")

        if white_player.lower() == target:
            player_color = "White"
        elif black_player.lower() == target:
            player_color = "Black"
        else:
            continue

        if chosen_color != "All" and player_color != chosen_color:
            continue

        line1, line2, flat_tree, branch_fen = get_opening_signature_and_fen(game, plies=4)

        indexed.append({
            "game_idx": idx,
            "raw_game": g,
            "parsed_game": game,
            "white": white_player,
            "black": black_player,
            "player_color": player_color,
            "date": game.headers.get("Date", "Unknown"),
            "line1": line1,
            "line2": line2,
            "opening_tree": flat_tree,
            "branch_fen": branch_fen
        })

    return indexed

def analyze_targeted_games(selected_records, target_username, min_drop, max_drop):
    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    engine.configure({"Threads": 2, "Hash": 128})

    leaks = []
    total = len(selected_records)
    progress_bar = st.progress(0, text="Auditing selected matches...")

    for i, rec in enumerate(selected_records):
        game = rec["parsed_game"]
        pgn_text = rec["raw_game"].get("pgn", "")
        my_color = chess.WHITE if rec["player_color"] == "White" else chess.BLACK
        is_flip = "false" if my_color == chess.WHITE else "true"

        board = game.board()
        moves = list(game.mainline_moves())
        clean_pgn = pgn_text.replace("\r\n", " ").replace("\n", " ").strip()
        encoded_pgn = quote(clean_pgn)

        info_current = engine.analyse(board, ENGINE_LIMIT)
        prev_eval = get_my_eval(info_current, my_color)

        for ply_index, move in enumerate(moves):
            move_num = (ply_index // 2) + 1
            if move_num > MAX_MOVE_NUMBER:
                break

            if board.turn == my_color:
                fen_before = board.fen()
                encoded_fen = quote(fen_before)

                chesscom_url = f"https://www.chess.com/analysis?fen={encoded_fen}&flip={is_flip}"
                lichess_url = f"https://lichess.org/analysis/pgn/{encoded_pgn}#{ply_index}"

                pv = info_current.get("pv", [])
                best_move = board.san(pv[0]) if pv else "N/A"
                played_move = board.san(move)

                board.push(move)
                info_next = engine.analyse(board, ENGINE_LIMIT)
                eval_after = get_my_eval(info_next, my_color)
                eval_drop = round(prev_eval - eval_after, 2)

                if min_drop <= eval_drop <= max_drop:
                    if eval_drop >= 1.50:
                        severity = "Blunder"
                        badge_color = "#FF4B4B"
                    elif eval_drop >= 0.80:
                        severity = "Mistake"
                        badge_color = "#FFA500"
                    else:
                        severity = "Inaccuracy"
                        badge_color = "#F0D000"

                    leaks.append({
                        "game_title": f"Game {rec['game_idx'] + 1}: {rec['white']} vs {rec['black']} ({rec['date']})",
                        "opening_tree": rec["opening_tree"],
                        "color": rec["player_color"],
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

        progress_bar.progress((i + 1) / total, text=f"Evaluating game {i + 1} of {total}...")

    engine.quit()
    return leaks

# --- SESSION STATE INITIALIZATION ---
if "indexed_games" not in st.session_state:
    st.session_state.indexed_games = None
if "audit_results" not in st.session_state:
    st.session_state.audit_results = None
if "audited_records_count" not in st.session_state:
    st.session_state.audited_records_count = 0
if "selected_trees_set" not in st.session_state:
    st.session_state.selected_trees_set = set()

# --- STREAMLIT UI ---
st.title("♟️ Chess Telemetry & Opening Leak Scanner")
st.caption("Visual opening frequency breakdown and game-by-game blunder audit.")

# --- SIDEBAR: DATA INGESTION ---
with st.sidebar:
    st.header("1. Ingestion Settings")
    username_input = st.text_input("Chess.com Username", value="danhonda")
    color_choice = st.radio("Analyze games as:", ["White", "Black", "All"], index=0)
    game_limit = st.slider("Matches to Fetch", min_value=5, max_value=60, value=25, step=5)

    fetch_btn = st.button("Fetch & Index Openings", type="primary")

    st.markdown("---")
    st.header("2. Blunder Calibration")
    eval_range = st.slider(
        "Evaluation Drop Range",
        min_value=0.20,
        max_value=3.50,
        value=(0.30, 1.50),
        step=0.10,
        help="Advantage lost in pawn equivalents."
    )
    min_drop, max_drop = eval_range

# --- STAGE 1: FETCH DATA ---
if fetch_btn and username_input:
    with st.spinner(f"Pulling recent matches for {username_input}..."):
        raw_games = fetch_chesscom_games(username_input, max_games=game_limit)

    if raw_games:
        indexed = index_games_metadata(raw_games, username_input, color_choice)
        st.session_state.indexed_games = indexed
        st.session_state.audit_results = None
        all_unique = {r["opening_tree"] for r in indexed}
        st.session_state.selected_trees_set = set(all_unique)

# --- STAGE 2: SELECTION & ANALYSIS ---
if st.session_state.indexed_games is not None:
    indexed = st.session_state.indexed_games

    if len(indexed) == 0:
        st.warning("No games found matching selected color.")
    else:
        index_df = pd.DataFrame(indexed)
        con = duckdb.connect()
        con.register("index_df", index_df)

        stats_query = """
        SELECT 
            opening_tree,
            FIRST(line1) as line1,
            FIRST(line2) as line2,
            FIRST(branch_fen) as sample_fen,
            FIRST(player_color) as sample_color,
            COUNT(*) AS count,
            ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 1) AS pct
        FROM index_df
        GROUP BY opening_tree
        ORDER BY count DESC
        """
        opening_stats = con.execute(stats_query).fetchall()
        all_trees = [item[0] for item in opening_stats]

        expander_open = st.session_state.audit_results is None
        with st.expander("⚙️ **Step 2: Select Opening Lines to Audit**", expanded=expander_open):
            btn_c1, btn_c2, _ = st.columns([1.5, 1.5, 5])
            if btn_c1.button("Select All", use_container_width=True):
                st.session_state.selected_trees_set = set(all_trees)
                st.rerun()
            if btn_c2.button("Deselect All", use_container_width=True):
                st.session_state.selected_trees_set = set()
                st.rerun()

            st.write("")
            cols = st.columns(3)

            for idx, (tree, l1, l2, fen, p_color, cnt, pct) in enumerate(opening_stats):
                target_col = cols[idx % 3]
                is_selected = tree in st.session_state.selected_trees_set

                with target_col:
                    with st.container(border=True):
                        # Tight 1.15 : 1.85 proportion so board and text fill the container
                        c_img, c_text = st.columns([1.15, 1.85], gap="small")
                        with c_img:
                            st.markdown(render_svg_board(fen, p_color, size=130), unsafe_allow_html=True)
                        with c_text:
                            st.markdown(
                                f"""
                                <div class="large-move-text">{l1}<br>{l2}</div>
                                <div class="large-stat-text">{cnt} game{'s' if cnt > 1 else ''} &bull; <b>{pct}%</b></div>
                                """,
                                unsafe_allow_html=True
                            )
                            checked = st.checkbox("Include Line", value=is_selected, key=f"chk_tree_{idx}")
                            if checked and not is_selected:
                                st.session_state.selected_trees_set.add(tree)
                                st.rerun()
                            elif not checked and is_selected:
                                st.session_state.selected_trees_set.discard(tree)
                                st.rerun()

            st.write("")
            start_audit = st.button("Run Deep Engine Audit on Selected Lines", type="primary")

            if start_audit:
                selected_trees = list(st.session_state.selected_trees_set)
                if not selected_trees:
                    st.warning("Please select at least one opening line.")
                else:
                    filtered = [r for r in indexed if r["opening_tree"] in selected_trees]
                    with st.spinner("Analyzing with Stockfish..."):
                        leaks = analyze_targeted_games(filtered, username_input, min_drop, max_drop)
                        st.session_state.audit_results = leaks
                        st.session_state.audited_records_count = len(filtered)
                    st.rerun()

# --- STAGE 3: AUDIT RESULTS ---
if st.session_state.audit_results is not None:
    leaks_data = st.session_state.audit_results
    total_audited = st.session_state.audited_records_count

    st.markdown("---")
    st.header("🎯 Engine Audit Results")

    if leaks_data:
        leaks_df = pd.DataFrame(leaks_data)
        con = duckdb.connect()
        con.register("leaks_df", leaks_df)

        # KPI Metrics
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Audited Matches", total_audited)
        k2.metric("Flagged Mistakes", len(leaks_data))
        avg_lost = con.execute("SELECT ROUND(AVG(eval_drop), 2) FROM leaks_df").fetchone()[0]
        k3.metric("Avg Pawn Loss / Error", f"-{avg_lost}")
        games_with_flaws = con.execute("SELECT COUNT(DISTINCT game_title) FROM leaks_df").fetchone()[0]
        k4.metric("Matches with Errors", games_with_flaws)

        # Game-by-Game Output
        st.subheader("🎮 Game-by-Game Breakdown")
        grouped_games = leaks_df.groupby("game_title")

        for game_title, group in grouped_games:
            opening_in_game = group.iloc[0]["opening_tree"]
            with st.expander(f"📌 **{game_title}** — [{opening_in_game}] — {len(group)} mistake(s)", expanded=True):
                for _, row in group.iterrows():
                    c_tag, c_mv, c_best, c_loss, c_links = st.columns([1.2, 1.4, 1.4, 1.4, 3.2])
                    c_tag.markdown(
                        f"<span style='background-color:{row['badge_color']}; color:black; font-weight:bold; padding:2px 8px; border-radius:4px;'>{row['severity']}</span>",
                        unsafe_allow_html=True
                    )
                    c_mv.markdown(f"**Move {row['move_number']}:** `{row['played_move']}`")
                    c_best.markdown(f"**Best:** `{row['engine_best']}`")
                    c_loss.markdown(f"**Drop:** `-{row['eval_drop']}`")
                    c_links.markdown(f"[♟️ Chess.com Board]({row['chesscom_url']}) | [📖 Lichess Move]({row['lichess_url']})")
    else:
        st.success(f"No leaks found within the range of {min_drop:.2f} to {max_drop:.2f} pawns for the selected lines.")
