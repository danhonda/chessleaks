def fetch_chesscom_games(username, max_games=100):
    headers = {"User-Agent": f"ChessLeakScanner/6.0 ({username}@portfolio.com)"}
    archives_url = f"https://api.chess.com/pub/player/{username.lower()}/games/archives"
    
    resp = requests.get(archives_url, headers=headers)
    if resp.status_code == 404:
        st.error(f"User '{username}' not found on Chess.com.")
        return []
    elif resp.status_code != 200:
        st.error(f"Failed to fetch player archives (Status {resp.status_code}).")
        return []

    archives = resp.json().get("archives", [])
    if not archives:
        st.warning(f"No game history found for '{username}'.")
        return []

    collected_games = []
    
    # Iterate backwards from the most recent month
    for month_url in reversed(archives):
        m_resp = requests.get(month_url, headers=headers)
        if m_resp.status_code == 200:
            month_games = m_resp.json().get("games", [])
            # Prepend newest games from this month
            collected_games = month_games + collected_games
            if len(collected_games) >= max_games:
                break

    # Return the most recent N games across all traversed months
    return collected_games[-max_games:]
