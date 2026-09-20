import sqlite3
import argparse
import sys
import shlex
import os

DB_NAME = "ime.db"

# 減衰（忘却）の定数
DECAY_THRESHOLD = 10
DECAY_PENALTY = 1
MAX_SCORE = 100
MIN_SCORE = 0

def init_db(conn):
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_data (
            key TEXT PRIMARY KEY,
            value INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS predict_scores (
            yomi TEXT,
            word TEXT,
            score INTEGER,
            last_used_count INTEGER,
            PRIMARY KEY (yomi, word)
        )
    ''')
    cursor.execute('INSERT OR IGNORE INTO meta_data (key, value) VALUES ("global_commit_count", 0)')
    conn.commit()

def get_global_commit_count(conn):
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM meta_data WHERE key="global_commit_count"')
    return cursor.fetchone()[0]

def increment_global_commit_count(conn):
    cursor = conn.cursor()
    cursor.execute('UPDATE meta_data SET value = value + 1 WHERE key="global_commit_count"')
    conn.commit()

def update_score(conn, yomi, word, delta, global_count=None):
    cursor = conn.cursor()
    cursor.execute('SELECT score FROM predict_scores WHERE yomi=? AND word=?', (yomi, word))
    row = cursor.fetchone()
    
    if row:
        new_score = max(MIN_SCORE, min(MAX_SCORE, row[0] + delta))
        if global_count is not None:
            cursor.execute('UPDATE predict_scores SET score=?, last_used_count=? WHERE yomi=? AND word=?', 
                           (new_score, global_count, yomi, word))
        else:
            cursor.execute('UPDATE predict_scores SET score=? WHERE yomi=? AND word=?', 
                           (new_score, yomi, word))
    else:
        new_score = max(MIN_SCORE, min(MAX_SCORE, delta))
        g_count = global_count if global_count is not None else get_global_commit_count(conn)
        cursor.execute('INSERT INTO predict_scores (yomi, word, score, last_used_count) VALUES (?, ?, ?, ?)', 
                       (yomi, word, new_score, g_count))
    conn.commit()

class IMESimulator:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        init_db(self.conn)
        self.action_history = []  # List of tuples: ("commit", yomi, word) or ("delete", )
    
    def get_global_count(self):
        return get_global_commit_count(self.conn)

    def do_commit(self, yomi, word):
        increment_global_commit_count(self.conn)
        g_count = self.get_global_count()
        
        # タイポ検知とショートカット学習: [..., COMMIT(A), DELETE, COMMIT(B)]
        if len(self.action_history) >= 2:
            last_action = self.action_history[-1]
            prev_action = self.action_history[-2]
            
            if last_action[0] == "delete" and prev_action[0] == "commit":
                typo_yomi = prev_action[1]
                # ショートカット学習: typo_yomi -> 新しいword
                update_score(self.conn, typo_yomi, word, 3, g_count)
                print(f"[*] タイポ検知: ショートカットを学習しました '{typo_yomi}' -> '{word}' (+3)")

        # 通常の確定（スコア+1）
        update_score(self.conn, yomi, word, 1, g_count)
        self.action_history.append(("commit", yomi, word))
        print(f"確定: {word} (よみ: {yomi})")

    def do_delete(self):
        # 直前の確定アクションを削除(BS)
        if not self.action_history:
            print("削除可能な履歴がありません。")
            return
        
        last_action = self.action_history[-1]
        if last_action[0] == "commit":
            yomi, word = last_action[1], last_action[2]
            update_score(self.conn, yomi, word, -2)
            self.action_history.append(("delete",))
            print(f"削除: {word} (よみ: {yomi}) のスコアを減少 (-2)")
        else:
            print("直前のアクションが確定ではないため削除できません。")

    def do_predict(self, yomi):
        g_count = self.get_global_count()
        cursor = self.conn.cursor()
        cursor.execute('SELECT word, score, last_used_count FROM predict_scores WHERE yomi=?', (yomi,))
        rows = cursor.fetchall()
        
        candidates = []
        for word, original_score, last_used in rows:
            gap = g_count - last_used
            # 一定回数以上使われていない場合はペナルティ減算
            decay = (gap // DECAY_THRESHOLD) * DECAY_PENALTY
            actual_score = max(MIN_SCORE, original_score - decay)
            candidates.append((word, actual_score, original_score, decay))
        
        # 減算後のスコアで降順ソート
        candidates.sort(key=lambda x: x[1], reverse=True)
        
        if not candidates:
            print(f"'{yomi}' の予測候補はありません。")
            return
        
        print(f"'{yomi}' の予測候補:")
        for word, score, orig_score, decay in candidates:
            decay_info = f" (元スコア: {orig_score}, 減衰: -{decay})" if decay > 0 else ""
            print(f"  - {word} : スコア {score}{decay_info}")

    def do_status(self):
        g_count = self.get_global_count()
        print(f"--- STATUS ---")
        print(f"総確定回数 (global_commit_count): {g_count}")
        cursor = self.conn.cursor()
        cursor.execute('SELECT yomi, word, score, last_used_count FROM predict_scores')
        rows = cursor.fetchall()
        print(f"DBレコード数: {len(rows)}")
        for y, w, s, l in rows:
            print(f"  よみ: {y}, 単語: {w} | スコア: {s}, 最終使用: {l}")
        
        print(f"直近のアクションログ (最大5件):")
        for a in self.action_history[-5:]:
            print(f"  {a}")
        print(f"--------------")

    def run(self):
        print("typo-learning-ime CLIシミュレーターを起動しました。'exit' で終了します。")
        while True:
            try:
                cmd_line = input("> ").strip()
                if not cmd_line:
                    continue
                
                parts = shlex.split(cmd_line)
                cmd = parts[0].lower()
                
                if cmd == "exit":
                    break
                elif cmd == "commit":
                    if len(parts) != 3:
                        print("使用法: commit <よみ> <単語>")
                        continue
                    self.do_commit(parts[1], parts[2])
                elif cmd == "delete":
                    self.do_delete()
                elif cmd == "predict":
                    if len(parts) != 2:
                        print("使用法: predict <よみ>")
                        continue
                    self.do_predict(parts[1])
                elif cmd == "status":
                    self.do_status()
                else:
                    print("不明なコマンドです。利用可能: commit, delete, predict, status, exit")
            except (EOFError, KeyboardInterrupt):
                print("\n終了します。")
                break
            except Exception as e:
                print(f"エラーが発生しました: {e}")

if __name__ == '__main__':
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_NAME)
    sim = IMESimulator(db_path)
    sim.run()
