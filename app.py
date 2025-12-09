import os
import json
import uuid
import threading
import subprocess
import shutil
import logging
import asyncio
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify, Response, session
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_cors import CORS
import yt_dlp
from spotdl import Spotdl
from spotdl.download.downloader import Downloader

app = Flask(__name__)
CORS(app)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# --- 設定 ---
app.config['BASE_DIR'] = os.path.dirname(os.path.abspath(__file__))
app.config['MUSIC_FOLDER'] = os.path.join(app.config['BASE_DIR'], 'music')
app.config['IMAGES_FOLDER'] = os.path.join(app.config['BASE_DIR'], 'images')
app.config['DATA_FOLDER'] = os.path.join(app.config['BASE_DIR'], 'data')
app.config['ARTISTS_FOLDER'] = os.path.join(app.config['DATA_FOLDER'], 'artists')
app.config['ALBUMS_FOLDER'] = os.path.join(app.config['DATA_FOLDER'], 'albums')
app.config['INDEX_FILE'] = os.path.join(app.config['DATA_FOLDER'], 'index.json')
app.config['UPLOAD_TEMP'] = os.path.join(app.config['BASE_DIR'], 'temp_upload')
app.config['SPOTDL_TEMP'] = os.path.join(app.config['BASE_DIR'], 'temp_spotdl')
app.config['LOG_FILE'] = os.path.join(app.config['BASE_DIR'], 'server.log')
app.config['KEY_FILE'] = os.path.join(app.config['BASE_DIR'], 'spotify_key.txt')

app.secret_key = 'super_secret_key_change_me'

# --- 認証情報 ---
ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = '123456'

ALLOWED_EXTENSIONS_IMG = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_EXTENSIONS_AUDIO = {'mp3', 'wav', 'm4a', 'aac', 'flac', 'mp4', 'mov', 'webm', 'mkv'}

# --- ログ設定 ---
logging.basicConfig(
    filename=app.config['LOG_FILE'],
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
console.setFormatter(formatter)
logging.getLogger('').addHandler(console)

# --- 初期化 ---
for folder in [app.config['MUSIC_FOLDER'], app.config['IMAGES_FOLDER'], app.config['DATA_FOLDER'], 
               app.config['ARTISTS_FOLDER'], app.config['ALBUMS_FOLDER'], app.config['UPLOAD_TEMP'],
               app.config['SPOTDL_TEMP']]:
    if not os.path.exists(folder):
        os.makedirs(folder)

if not os.path.exists(app.config['INDEX_FILE']):
    with open(app.config['INDEX_FILE'], 'w', encoding='utf-8') as f:
        json.dump([], f)

# --- Spotify APIキー読み込み ---
SPOTIFY_CLIENT_ID = None
SPOTIFY_CLIENT_SECRET = None

def load_spotify_keys():
    global SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
    if os.path.exists(app.config['KEY_FILE']):
        try:
            with open(app.config['KEY_FILE'], 'r', encoding='utf-8') as f:
                lines = f.read().splitlines()
                if len(lines) >= 2:
                    SPOTIFY_CLIENT_ID = lines[0].strip()
                    SPOTIFY_CLIENT_SECRET = lines[1].strip()
                    logging.info("Spotify keys loaded successfully.")
                else:
                    logging.warning("spotify_key.txt format invalid (needs 2 lines).")
        except Exception as e:
            logging.error(f"Failed to load spotify_key.txt: {e}")
    else:
        logging.warning("spotify_key.txt not found.")

load_spotify_keys()

# --- Global SpotDL Client (検索用) ---
# アプリ起動時に一度だけ初期化することで「Already initialized」エラーを防ぐ
spotify_search_client = None
if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        spotify_search_client = Spotdl(
            client_id=SPOTIFY_CLIENT_ID, 
            client_secret=SPOTIFY_CLIENT_SECRET, 
            user_auth=False, 
            headless=True
        )
    except Exception as e:
        logging.error(f"Failed to initialize global SpotDL client: {e}")

# --- 認証・ヘルパー関数 ---

def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response('認証が必要です', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

def load_index():
    try:
        with open(app.config['INDEX_FILE'], 'r', encoding='utf-8') as f: return json.load(f)
    except: return []

def save_index(data):
    with open(app.config['INDEX_FILE'], 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

def load_artist(artist_id):
    path = os.path.join(app.config['ARTISTS_FOLDER'], f"{artist_id}.json")
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f: return json.load(f)
    return None

def save_artist(data):
    path = os.path.join(app.config['ARTISTS_FOLDER'], f"{data['id']}.json")
    with open(path, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)
    idx = load_index()
    summary = {
        "id": data['id'], "name": data['name'], "genre": data.get('genre', ''),
        "description": data.get('description', ''), "image": data.get('image', ''),
        "album_count": len(data['albums'])
    }
    found = False
    for i, item in enumerate(idx):
        if item['id'] == data['id']:
            idx[i] = summary; found = True; break
    if not found: idx.append(summary)
    save_index(idx)

def load_album(album_id):
    path = os.path.join(app.config['ALBUMS_FOLDER'], f"{album_id}.json")
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f: return json.load(f)
    return None

def save_album(data):
    path = os.path.join(app.config['ALBUMS_FOLDER'], f"{data['id']}.json")
    # 【重要】保存時に必ず数値としてソートする
    if 'tracks' in data:
        data['tracks'].sort(key=lambda x: int(x.get('track_number', 0)))
    with open(path, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)

def delete_artist_data(artist_id):
    artist = load_artist(artist_id)
    if artist:
        for alb in artist['albums']:
            p = os.path.join(app.config['ALBUMS_FOLDER'], f"{alb['id']}.json")
            if os.path.exists(p): os.remove(p)
        p = os.path.join(app.config['ARTISTS_FOLDER'], f"{artist_id}.json")
        if os.path.exists(p): os.remove(p)
    idx = load_index()
    idx = [a for a in idx if a['id'] != artist_id]
    save_index(idx)

def delete_album_data(artist_id, album_id):
    p = os.path.join(app.config['ALBUMS_FOLDER'], f"{album_id}.json")
    if os.path.exists(p): os.remove(p)
    artist = load_artist(artist_id)
    if artist:
        artist['albums'] = [a for a in artist['albums'] if a['id'] != album_id]
        save_artist(artist)

def allowed_image(f): return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS_IMG
def allowed_audio(f): return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS_AUDIO

def save_image_file(file):
    if file and allowed_image(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(app.config['IMAGES_FOLDER'], filename))
        return filename
    return None

def process_upload_file(file):
    filename = secure_filename(file.filename)
    base_id = uuid.uuid4().hex
    temp_path = os.path.join(app.config['UPLOAD_TEMP'], f"{base_id}_{filename}")
    file.save(temp_path)
    final_filename = f"{base_id}.mp3"
    hq_path = os.path.join(app.config['MUSIC_FOLDER'], final_filename)
    try:
        subprocess.run(['ffmpeg', '-y', '-i', temp_path, '-b:a', '320k', '-map', 'a', hq_path],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logging.error(f"File convert error: {e}")
        if os.path.exists(temp_path): os.remove(temp_path)
        return None
    if os.path.exists(temp_path): os.remove(temp_path)
    return final_filename

# --- バックグラウンド処理 (YouTube) ---

def background_youtube_process(album_id, url, temp_track_id, start_track_num):
    logging.info(f"Start YouTube DL: {url}")
    try:
        ydl_opts_info = {'quiet': True, 'extract_flat': 'in_playlist', 'ignoreerrors': True}
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info: raise Exception("Info fetch failed")
        if 'entries' in info: entries = list(info['entries'])
        else: entries = [info]

        album = load_album(album_id)
        if not album: return

        if temp_track_id:
            album['tracks'] = [t for t in album['tracks'] if t['id'] != temp_track_id]

        download_queue = []
        current_num = start_track_num

        for entry in entries:
            if not entry: continue
            track_id = str(uuid.uuid4())
            title = entry.get('track') or entry.get('title', 'Unknown Title')
            video_url = entry.get('url') or entry.get('webpage_url')
            
            placeholder = {
                "id": track_id, "title": f"【待機中】 {title}", "track_number": int(current_num),
                "filename": None, "processing": True, "status": "pending",
                "source_type": "youtube", "original_url": video_url
            }
            album['tracks'].append(placeholder)
            download_queue.append(placeholder)
            current_num += 1
        
        save_album(album)

        ydl_opts_dl = {
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '320'}],
            'quiet': True, 'ignoreerrors': True
        }

        for item in download_queue:
            album = load_album(album_id)
            if not album: break
            target = next((t for t in album['tracks'] if t['id'] == item['id']), None)
            if not target: continue

            target['title'] = f"【DL中...】 {item['title'].replace('【待機中】 ', '')}"
            target['status'] = "downloading"
            save_album(album)

            try:
                base_id = uuid.uuid4().hex
                save_path_base = os.path.join(app.config['MUSIC_FOLDER'], base_id)
                current_opts = ydl_opts_dl.copy()
                current_opts['outtmpl'] = save_path_base

                with yt_dlp.YoutubeDL(current_opts) as ydl:
                    dl_info = ydl.extract_info(item['original_url'], download=True)
                    if not dl_info: raise Exception("Download failed")
                    real_title = dl_info.get('track') or dl_info.get('title', 'Unknown Title')

                target['title'] = real_title
                target['filename'] = f"{base_id}.mp3"
                target['status'] = "completed"
                if 'processing' in target: del target['processing']
                save_album(album)
                logging.info(f"YouTube DL Success: {real_title}")

            except Exception as e:
                logging.error(f"YouTube DL Error ({item['original_url']}): {e}")
                target['title'] = f"【エラー】 {item['title'].replace('【待機中】 ', '')}"
                target['status'] = "error"
                target['error_msg'] = str(e)
                if 'processing' in target: del target['processing']
                save_album(album)

    except Exception as e:
        logging.critical(f"YouTube Critical Error: {e}")
        try:
            album = load_album(album_id)
            if album and temp_track_id:
                target = next((t for t in album['tracks'] if t['id'] == temp_track_id), None)
                if target:
                    target['title'] = "【初期化エラー】"
                    target['status'] = "error"
                    target['error_msg'] = str(e)
                    if 'processing' in target: del target['processing']
                    save_album(album)
        except: pass

# --- バックグラウンド処理 (Spotify) ---

def background_spotify_process(album_id, url, temp_track_id, start_track_num):
    # 【重要】スレッドごとに新しいイベントループを作成
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    logging.info(f"Start Spotify DL: {url}")
    try:
        if not spotify_search_client:
            raise Exception("Spotify Client is not initialized. Check spotify_key.txt")
        
        try:
            # 検索にはグローバルのクライアントを使用（スレッドセーフな同期メソッドとして使用）
            songs = spotify_search_client.search([url])
        except Exception as e:
            raise Exception(f"Search failed: {e}")

        album = load_album(album_id)
        if not album: return

        if temp_track_id:
            album['tracks'] = [t for t in album['tracks'] if t['id'] != temp_track_id]

        download_queue = []
        current_num = start_track_num

        for song in songs:
            track_id = str(uuid.uuid4())
            song_title = song.name
            
            placeholder = {
                "id": track_id, "title": f"【待機中】 {song_title}", "track_number": int(current_num),
                "filename": None, "processing": True, "status": "pending",
                "source_type": "spotify", "original_url": song.url
            }
            album['tracks'].append(placeholder)
            download_queue.append((placeholder, song))
            current_num += 1
        
        save_album(album)

        # ダウンロード用の設定
        dl_settings = {
            "headless": True,
            "simple_tui": True,
            "audio_providers": ["youtube-music", "youtube"], # 音源ソース指定
        }

        # ダウンロードループ
        for item_dict, song_obj in download_queue:
            album = load_album(album_id)
            if not album: break
            target = next((t for t in album['tracks'] if t['id'] == item_dict['id']), None)
            if not target: continue

            target['title'] = f"【DL中...】 {song_obj.name}"
            target['status'] = "downloading"
            save_album(album)

            try:
                base_id = uuid.uuid4().hex
                temp_dl_dir = os.path.join(app.config['SPOTDL_TEMP'], base_id)
                if not os.path.exists(temp_dl_dir): os.makedirs(temp_dl_dir)
                
                # 【重要】このスレッドのループを使う新しいDownloaderを作成
                downloader = Downloader(settings=dl_settings, loop=loop)
                downloader.settings["output"] = os.path.join(temp_dl_dir, "{artists} - {title}.{output-ext}")
                
                # ダウンロード実行
                result = downloader.download_song(song_obj)
                
                if result:
                    # spotdl v4.2+ では (song, path) を返す
                    if isinstance(result, tuple):
                        _, path_obj = result
                        dl_file = str(path_obj)
                    else:
                        dl_file = str(result)

                    final_path = os.path.join(app.config['MUSIC_FOLDER'], f"{base_id}.mp3")
                    
                    subprocess.run([
                        'ffmpeg', '-y', '-i', dl_file,
                        '-b:a', '320k', '-map', 'a',
                        final_path
                    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    if os.path.exists(temp_dl_dir): shutil.rmtree(temp_dl_dir)

                    target['title'] = song_obj.name
                    target['filename'] = f"{base_id}.mp3"
                    target['status'] = "completed"
                    if 'processing' in target: del target['processing']
                    save_album(album)
                    logging.info(f"Spotify DL Success: {song_obj.name}")
                else:
                    raise Exception("No file returned from downloader")

            except Exception as e:
                logging.error(f"Spotify DL Error ({song_obj.name}): {e}")
                target['title'] = f"【エラー】 {song_obj.name}"
                target['status'] = "error"
                target['error_msg'] = str(e)
                if 'processing' in target: del target['processing']
                save_album(album)
                if os.path.exists(os.path.join(app.config['SPOTDL_TEMP'], base_id)):
                    shutil.rmtree(os.path.join(app.config['SPOTDL_TEMP'], base_id))
    
    except Exception as e:
        logging.critical(f"Spotify Critical Error: {e}")
        try:
            album = load_album(album_id)
            if album and temp_track_id:
                target = next((t for t in album['tracks'] if t['id'] == temp_track_id), None)
                if target:
                    target['title'] = "【初期化エラー】"
                    target['status'] = "error"
                    target['error_msg'] = str(e)
                    if 'processing' in target: del target['processing']
                    save_album(album)
        except: pass
    finally:
        try: loop.close()
        except: pass

# --- API / Routes ---

@app.route('/stream/<path:filename>')
def stream_music(filename):
    return send_from_directory(app.config['MUSIC_FOLDER'], filename)

@app.route('/image/<path:filename>')
def serve_image(filename):
    return send_from_directory(app.config['IMAGES_FOLDER'], filename)

@app.route('/api/artists')
def api_get_artists():
    data = load_index()
    for artist in data:
        if artist.get('image'):
            artist['image_url'] = url_for('serve_image', filename=artist['image'], _external=True, _scheme='https')
        artist['api_url'] = url_for('api_get_artist_detail', artist_id=artist['id'], _external=True, _scheme='https')
    return jsonify(data)

@app.route('/api/artist/<artist_id>')
def api_get_artist_detail(artist_id):
    artist = load_artist(artist_id)
    if not artist: return jsonify({"error": "Artist not found"}), 404
    if artist.get('image'):
        artist['image_url'] = url_for('serve_image', filename=artist['image'], _external=True, _scheme='https')
    for album in artist['albums']:
        if album.get('cover_image'):
            album['cover_url'] = url_for('serve_image', filename=album['cover_image'], _external=True, _scheme='https')
        album['api_url'] = url_for('api_get_album_detail', album_id=album['id'], _external=True, _scheme='https')
    return jsonify(artist)

@app.route('/api/album/<album_id>')
def api_get_album_detail(album_id):
    album = load_album(album_id)
    if not album: return jsonify({"error": "Album not found"}), 404
    if album.get('cover_image'):
        album['cover_url'] = url_for('serve_image', filename=album['cover_image'], _external=True, _scheme='https')
    for track in album['tracks']:
        if track.get('status') == 'completed' and track.get('filename'):
            track['stream_url'] = url_for('stream_music', filename=track['filename'], _external=True, _scheme='https')
        track['cover_url'] = album.get('cover_url')
    return jsonify(album)

# --- Admin Routes ---

@app.route('/')
def root_redirect(): return redirect('/admin/')

@app.route('/admin/')
@requires_auth
def admin_index(): return render_template('index.html', artists=load_index())

@app.route('/admin/artist/add', methods=['POST'])
@requires_auth
def admin_add_artist():
    img = save_image_file(request.files.get('image'))
    new_artist = {"id": str(uuid.uuid4()), "name": request.form['name'], "genre": request.form.get('genre',''), "description": request.form.get('description',''), "image": img, "albums": []}
    save_artist(new_artist)
    return redirect(url_for('admin_index'))

@app.route('/admin/artist/<artist_id>/edit', methods=['POST'])
@requires_auth
def admin_edit_artist(artist_id):
    a = load_artist(artist_id)
    if a:
        a['name'] = request.form['name']; a['genre'] = request.form['genre']; a['description'] = request.form['description']
        img = save_image_file(request.files.get('image'))
        if img: a['image'] = img
        save_artist(a)
    return redirect(url_for('admin_index'))

@app.route('/admin/artist/<artist_id>/delete', methods=['POST'])
@requires_auth
def admin_delete_artist(artist_id):
    delete_artist_data(artist_id)
    return redirect(url_for('admin_index'))

@app.route('/admin/artist/<artist_id>')
@requires_auth
def admin_view_artist(artist_id):
    a = load_artist(artist_id)
    if not a: return "Not found", 404
    return render_template('artist.html', artist=a)

@app.route('/admin/artist/<artist_id>/album/add', methods=['POST'])
@requires_auth
def admin_add_album(artist_id):
    a = load_artist(artist_id)
    if a:
        aid = str(uuid.uuid4())
        img = save_image_file(request.files.get('image'))
        a['albums'].append({"id": aid, "title": request.form['title'], "year": request.form.get('year',''), "type": request.form.get('type','Album'), "cover_image": img})
        save_artist(a)
        save_album({"id": aid, "artist_id": artist_id, "artist_name": a['name'], "title": request.form['title'], "year": request.form.get('year',''), "type": request.form.get('type','Album'), "cover_image": img, "tracks": []})
    return redirect(url_for('admin_view_artist', artist_id=artist_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/edit', methods=['POST'])
@requires_auth
def admin_edit_album(artist_id, album_id):
    a = load_artist(artist_id); alb = load_album(album_id)
    if a and alb:
        t, y, tp = request.form['title'], request.form['year'], request.form['type']
        img = save_image_file(request.files.get('image'))
        for r in a['albums']:
            if r['id'] == album_id:
                r['title'] = t; r['year'] = y; r['type'] = tp
                if img: r['cover_image'] = img
        save_artist(a)
        alb['title'] = t; alb['year'] = y; alb['type'] = tp
        if img: alb['cover_image'] = img
        save_album(alb)
    return redirect(url_for('admin_view_artist', artist_id=artist_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/delete', methods=['POST'])
@requires_auth
def admin_delete_album(artist_id, album_id):
    delete_album_data(artist_id, album_id)
    return redirect(url_for('admin_view_artist', artist_id=artist_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>')
@requires_auth
def admin_view_album(artist_id, album_id):
    a = load_artist(artist_id); alb = load_album(album_id)
    if not a or not alb: return "Not found", 404
    return render_template('album.html', artist=a, album=alb)

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/add', methods=['POST'])
@requires_auth
def admin_add_track(artist_id, album_id):
    if 'file' not in request.files: return "No file", 400
    file = request.files['file']
    if not file.filename: return "No filename", 400
    
    fname = process_upload_file(file)
    if not fname: return "Error", 500
    
    alb = load_album(album_id)
    if alb:
        tn = request.form.get('track_number') or len(alb['tracks']) + 1
        alb['tracks'].append({
            "id": str(uuid.uuid4()), "title": request.form.get('title') or file.filename,
            "track_number": int(tn), "filename": fname, "status": "completed", "source_type": "upload"
        })
        save_album(alb)
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/add_url', methods=['POST'])
@requires_auth
def admin_add_track_url(artist_id, album_id):
    url = request.form.get('url')
    source = request.form.get('source', 'youtube')
    
    alb = load_album(album_id)
    if not alb: return "Error", 404

    tn = int(request.form.get('track_number') or len(alb['tracks']) + 1)
    tid = str(uuid.uuid4())
    
    alb['tracks'].append({
        "id": tid, "title": "初期化中...", "track_number": tn, "filename": None,
        "processing": True, "status": "pending", "source_type": source, "original_url": url
    })
    save_album(alb)

    if source == 'spotify':
        t = threading.Thread(target=background_spotify_process, args=(album_id, url, tid, tn))
    else:
        t = threading.Thread(target=background_youtube_process, args=(album_id, url, tid, tn))
    t.start()
    
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/<track_id>/retry', methods=['POST'])
@requires_auth
def admin_retry_track(artist_id, album_id, track_id):
    alb = load_album(album_id)
    if not alb: return "Error", 404
    
    target = next((t for t in alb['tracks'] if t['id'] == track_id), None)
    if not target: return "Track not found", 404
    
    if target.get('status') == 'error':
        target['status'] = 'pending'
        target['processing'] = True
        target['title'] = f"【再試行中】 {target.get('title', '').replace('【エラー】 ', '')}"
        save_album(alb)
        
        url = target.get('original_url')
        source = target.get('source_type', 'youtube')
        tn = target.get('track_number')
        
        if source == 'spotify':
            t = threading.Thread(target=background_spotify_process, args=(album_id, url, None, tn))
        else:
            t = threading.Thread(target=background_youtube_process, args=(album_id, url, None, tn))
        t.start()
        
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/retry_all', methods=['POST'])
@requires_auth
def admin_retry_all(artist_id, album_id):
    alb = load_album(album_id)
    if not alb: return "Error", 404
    
    error_tracks = [t for t in alb['tracks'] if t.get('status') == 'error']
    for target in error_tracks:
        target['status'] = 'pending'
        target['processing'] = True
        target['title'] = f"【一括再試行】 {target.get('title', '').replace('【エラー】 ', '')}"
        save_album(alb)
        
        url = target.get('original_url')
        source = target.get('source_type', 'youtube')
        tn = target.get('track_number')
        
        if source == 'spotify':
            t = threading.Thread(target=background_spotify_process, args=(album_id, url, None, tn))
        else:
            t = threading.Thread(target=background_youtube_process, args=(album_id, url, None, tn))
        t.start()
        asyncio.run(asyncio.sleep(0.5))

    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/<track_id>/edit', methods=['POST'])
@requires_auth
def admin_edit_track(artist_id, album_id, track_id):
    alb = load_album(album_id)
    if alb:
        t = next((x for x in alb['tracks'] if x['id'] == track_id), None)
        if t:
            t['title'] = request.form['title']; t['track_number'] = int(request.form['track_number'])
            save_album(alb)
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/<track_id>/delete', methods=['POST'])
@requires_auth
def admin_delete_track(artist_id, album_id, track_id):
    alb = load_album(album_id)
    if alb:
        t = next((x for x in alb['tracks'] if x['id'] == track_id), None)
        if t and t.get('filename'):
            p = os.path.join(app.config['MUSIC_FOLDER'], t['filename'])
            if os.path.exists(p): os.remove(p)
        alb['tracks'] = [x for x in alb['tracks'] if x['id'] != track_id]
        save_album(alb)
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
