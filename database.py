import sqlite3
from datetime import datetime

class Database:
    def __init__(self, db_file="bot.db"):
        self.conn = sqlite3.connect(db_file)
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        
        # Таблица конкурсов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                channels TEXT,
                winners_count INTEGER,
                created_at TIMESTAMP,
                admin_id INTEGER,
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        
        # Таблица участников
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id INTEGER,
                user_id INTEGER,
                joined_at TIMESTAMP,
                PRIMARY KEY (giveaway_id, user_id),
                FOREIGN KEY (giveaway_id) REFERENCES giveaways (id)
            )
        ''')
        
        # Таблица победителей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS winners (
                giveaway_id INTEGER,
                user_id INTEGER,
                selected_at TIMESTAMP,
                FOREIGN KEY (giveaway_id) REFERENCES giveaways (id)
            )
        ''')
        
        # Таблица заявок пиар-менеджеров
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pr_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                age INTEGER,
                nickname TEXT,
                chats_count INTEGER,
                proof TEXT,
                status TEXT DEFAULT 'pending',
                applied_at TIMESTAMP
            )
        ''')
        
        self.conn.commit()
    
    # Методы для работы с конкурсами
    def add_giveaway(self, name, channels, winners_count, admin_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO giveaways (name, channels, winners_count, created_at, admin_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, ','.join(map(str, channels)), winners_count, datetime.now(), admin_id))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_giveaway(self, giveaway_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM giveaways WHERE id = ?', (giveaway_id,))
        row = cursor.fetchone()
        if row:
            return {
                'id': row[0],
                'name': row[1],
                'channels': list(map(int, row[2].split(','))) if row[2] else [],
                'winners_count': row[3],
                'created_at': row[4],
                'admin_id': row[5],
                'is_active': bool(row[6])
            }
        return None
    
    # Методы для работы с участниками
    def add_participant(self, giveaway_id, user_id):
        cursor = self.conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO participants (giveaway_id, user_id, joined_at)
                VALUES (?, ?, ?)
            ''', (giveaway_id, user_id, datetime.now()))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
    
    def get_participants(self, giveaway_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT user_id FROM participants WHERE giveaway_id = ?', (giveaway_id,))
        return [row[0] for row in cursor.fetchall()]
    
    def close(self):
        self.conn.close()
