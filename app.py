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

# --- HELPER FUNCTIONS ---
def get_my_eval(info, my_color):
    score = info["score"].white() if my_color == chess.WHITE else info["score"].black()
    if score.is_mate():
        return 100.0 if score.mate() > 0 else -100.0
    return (score.score() or 0) / 100.0

def get_opening_signature_and_fen(game, plies=4):
    """Extracts first 2 full moves (4 plies) and generates the resulting FEN."""
    temp_board = game.board()
    moves = list(game.mainline_moves())[:plies]
    tokens = []
    for idx, move in enumerate(moves):
        if idx % 2 == 0:
            tokens.append(f"{(idx // 2) + 1}.")
        tokens.append(temp_board.san(move))
        temp_board.push(move)
    sig = " ".join(tokens) if tokens else "Unknown Setup"
    return sig, temp_board.fen()

def render_svg_board(fen, player_color, size=150):
    """Renders a python-chess board as an embedded base64 SVG image."""
    b = chess.Board(fen)
    orientation = chess.WHITE if player_color == "White" else chess.BLACK
    svg_data = chess.svg.board(board=b, orientation=orientation, size=size)
    b64 = base64.b64encode(svg_data.encode("utf-8")).decode("utf-8")
    return f'<img src="data:image/svg+xml;base64,{b64}" width="{size}" style="border-radius:6px; border:1px solid #444;" />'

def fetch_chesscom_games(username, max_games=30):
    now = datetime.datetime.now()
    year = now.strftime("%Y")
    month = now.strftime("%m")
    
    url = f"https://api.chess.com/pub/player/{username.lower()}/games/{year}/{month}"
    headers = {"User-Agent": f"ChessLeakScanner/4.0 ({username}@portfolio.com)"}
    
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

        opening_tree, branch_fen = get_opening_signature_and_fen(game, plies=4)
        raw_header = game.headers.get("Opening", "")
        if raw_header and raw_header != "Unknown":
            display_label = f"{opening_tree} ({raw_header})"
        else:
            display_label = opening_tree

        indexed.append({
            "game_idx": idx,
            "raw_game": g,
            "parsed_game": game,
            "white": white_player,
            "black": black_player,
            "player_color": player_color,
            "date": game.headers.get("Date", "Unknown"),
            "opening_tree": opening_tree,
            "branch_fen": branch_fen,
            "display_label": display_label
        })

    return indexed

def analyze_targeted_games(selected_records, target_username, min_drop, max_drop):
    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    engine.configure({"Threads": 2, "Hash": 128})

    leaks = []
    total = len(selected_records)
    progress_bar = st.progress(0, text="Auditing selected opening lines...")

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
if "username_indexed" not in st.session_state:
    st.session_state.username_indexed = ""
if "color_indexed" not in st.session_state:
    st.session_state.color_indexed = ""

# --- STREAMLIT UI ---
st.title("♟️ Chess Telemetry & Opening Leak Scanner")
st.caption("Visual opening risk profiling and game-by-game blunder breakdown.")

# --- SIDEBAR: STAGE 1 SETUP ---
with st.sidebar:
    st.header("1. Data Ingestion")
    username_input = st.text_input("Chess.com Username", value="danhonda")
    color_choice = st.radio("Analyze games played as:", ["White", "Black", "All"], index=0)
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
        help="Loss threshold in pawn equivalents (e.g., 0.3 - 0.9 for slight mistakes)."
    )
    min_drop, max_drop = eval_range

# --- PIPELINE LOGIC ---
if fetch_btn and username_input:
    with st.spinner(f"Pulling recent matches for {username_input}..."):
        raw_games = fetch_chesscom_games(username_input, max_games=game_limit)

    if raw_games:
        indexed = index_games_metadata(raw_games, username_input, color_choice)
        st.session_state.indexed_games = indexed
        st.session_state.username_indexed = username_input
        st.session_state.color_indexed = color_choice

if st.session_state.indexed_games is not None:
    indexed = st.session_state.indexed_games
    total_indexed = len(indexed)

    if total_indexed == 0:
        st.warning(f"No games found matching color '{st.session_state.color_indexed}'.")
    else:
        st.subheader("Step 2: Select Opening Lines to Audit")
        
        index_df = pd.DataFrame(indexed)
        con = duckdb.connect()
        con.register("index_df", index_df)

        stats_query = """
        SELECT 
            opening_tree,
            FIRST(branch_fen) as sample_fen,
            FIRST(player_color) as sample_color,
            COUNT(*) AS count,
            ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 1) AS pct
        FROM index_df
        GROUP BY opening_tree
        ORDER BY count DESC
        """
        opening_stats = con.execute(stats_query).fetchall()

        c_actions, _ = st.columns([2, 5])
        select_all = c_actions.checkbox("Select All Openings", value=True)

        with st.form("audit_form"):
            st.write("Choose lines to analyze with Stockfish:")
            selected_labels = []

            for row_idx, (tree, fen, p_color, cnt, pct) in enumerate(opening_stats):
                col_box, col_img = st.columns([3.5, 1.5])
                
                with col_box:
                    st.markdown(f"### {tree}")
                    st.write(f"**Frequency:** {cnt} match{'es' if cnt > 1 else ''} ({pct}% of sample)")
                    checked = st.checkbox("Include this line", value=select_all, key=f"tree_{tree}_{row_idx}")
                    if checked:
                        selected_labels.append(tree)

                with col_img:
                    board_html = render_svg_board(fen, p_color, size=150)
                    st.markdown(board_html, unsafe_allow_html=True)

                st.markdown("<hr style='margin: 10px 0; border: 0.5px solid #333;'>", unsafe_allow_html=True)

            submit_audit = st.form_submit_button("Run Deep Engine Audit on Selected Lines", type="primary")

        # --- STAGE 2: TARGETED STOCKFISH SCAN ---
        if submit_audit:
            if not selected_labels:
                st.warning("Please check at least one opening sequence.")
            else:
                filtered_records = [r for r in indexed if r["opening_tree"] in selected_labels]
                st.info(f"Analyzing {len(filtered_records)} game(s) matching selected branches...")

                leaks_data = analyze_targeted_games(
                    filtered_records, 
                    st.session_state.username_indexed, 
                    min_drop, 
                    max_drop
                )

                if leaks_data:
                    leaks_df = pd.DataFrame(leaks_data)
                    con.register("leaks_df", leaks_df)

                    # KPI Cards
                    k1, k2, k3, k4 = st.columns(4)
                    k1.metric("Audited Matches", len(filtered_records))
                    k2.metric("Flagged Mistakes", len(leaks_data))
                    avg_lost = con.execute("SELECT ROUND(AVG(eval_drop), 2) FROM leaks_df").fetchone()[0]
                    k3.metric("Avg Pawn Loss / Error", f"-{avg_lost}")
                    games_with_flaws = con.execute("SELECT COUNT(DISTINCT game_title) FROM leaks_df").fetchone()[0]
                    k4.metric("Games with Mistakes", games_with_flaws)

                    st.markdown("---")
                    st.subheader("📊 Opening Vulnerability Summary (DuckDB)")

                    summary_query = """
                    SELECT 
                        color AS "Color",
                        opening_tree AS "Opening Line",
                        COUNT(*) AS "Total Mistakes",
                        ROUND(AVG(eval_drop), 2) AS "Avg Pawn Loss",
                        MAX(eval_drop) AS "Worst Blunder"
                    FROM leaks_df
                    GROUP BY color, opening_tree
                    ORDER BY "Total Mistakes" DESC, "Avg Pawn Loss" DESC
                    """
                    st.dataframe(con.execute(summary_query).df(), use_container_width=True, hide_index=True)

                    st.markdown("---")
                    st.subheader("🎮 Game-by-Game Audit Breakdown")

                    # Group results strictly by match
                    grouped_games = leaks_df.groupby("game_title")

                    for game_title, group in grouped_games:
                        opening_in_game = group.iloc[0]["opening_tree"]
                        with st.expander(f"📌 **{game_title}** — [{opening_in_game}] — {len(group)} mistake(s)", expanded=True):
                            for _, row in group.iterrows():
                                c_tag, c_mv, c_best, c_loss, c_links = st.columns([1.2, 1.5, 1.5, 1.5, 3.2])
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
