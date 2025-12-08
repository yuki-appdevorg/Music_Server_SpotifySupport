import os
import json
import uuid
import threading
import time
import subprocess
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify, Response, session
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_cors import CORS
import yt_dlp
from spotdl import Spotdl
from spotdl.types.song import Song

app = Flask(__name__)
CORS(app)

# プロキシ対応
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

app.secret_key = 'super_secret_key_change_me'

# === 【重要】ここにSpotifyのキーを入力してください ===
SPOTIFY_CLIENT_ID = 'ここにClient_IDを貼り付け'
SPOTIFY_CLIENT_SECRET = 'ここにClient_Secretを貼り付け'
# ==================================================

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = '123456'

ALLOWED_EXTENSIONS_IMG = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_EXTENSIONS_AUDIO = {'mp3', 'wav', 'm4a', 'aac', 'flac', 'mp4', 'mov', 'webm', 'mkv'}

for folder in [app.config['MUSIC_FOLDER'], app.config['IMAGES_FOLDER'], app.config['DATA_FOLDER'], 
               app.config['ARTISTS_FOLDER'], app.config['ALBUMS_FOLDER'], app.config['UPLOAD_TEMP']]:
    if not os.path.exists(folder):
        os.makedirs(folder)

if not os.path.exists(app.config['INDEX_FILE']):
    with open(app.config['INDEX_FILE'], 'w', encoding='utf-8') as f:
        json.dump([], f)

# --- 認証 ---
def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response(
        '認証が必要です', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# --- データ操作 ---
def load_index():
    try:
        with open(app.config['INDEX_FILE'], 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError: return []

def save_index(data):
    with open(app.config['INDEX_FILE'], 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_artist(artist_id):
    filepath = os.path.join(app.config['ARTISTS_FOLDER'], f"{artist_id}.json")
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def save_artist(artist_data):
    filepath = os.path.join(app.config['ARTISTS_FOLDER'], f"{artist_data['id']}.json")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(artist_data, f, indent=4, ensure_ascii=False)
    
    index_data = load_index()
    summary = {
        "id": artist_data['id'],
        "name": artist_data['name'],
        "genre": artist_data.get('genre', ''),
        "description": artist_data.get('description', ''),
        "image": artist_data.get('image', ''),
        "album_count": len(artist_data['albums'])
    }
    found = False
    for i, item in enumerate(index_data):
        if item['id'] == artist_data['id']:
            index_data[i] = summary
            found = True
            break
    if not found: index_data.append(summary)
    save_index(index_data)

def load_album(album_id):
    filepath = os.path.join(app.config['ALBUMS_FOLDER'], f"{album_id}.json")
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def save_album(album_data):
    filepath = os.path.join(app.config['ALBUMS_FOLDER'], f"{album_data['id']}.json")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(album_data, f, indent=4, ensure_ascii=False)

def delete_artist_data(artist_id):
    artist = load_artist(artist_id)
    if artist:
        for alb_ref in artist['albums']:
            alb_path = os.path.join(app.config['ALBUMS_FOLDER'], f"{alb_ref['id']}.json")
            if os.path.exists(alb_path): os.remove(alb_path)
        art_path = os.path.join(app.config['ARTISTS_FOLDER'], f"{artist_id}.json")
        if os.path.exists(art_path): os.remove(art_path)
    index_data = load_index()
    index_data = [a for a in index_data if a['id'] != artist_id]
    save_index(index_data)

def delete_album_data(artist_id, album_id):
    alb_path = os.path.join(app.config['ALBUMS_FOLDER'], f"{album_id}.json")
    if os.path.exists(alb_path): os.remove(alb_path)
    artist = load_artist(artist_id)
    if artist:
        artist['albums'] = [a for a in artist['albums'] if a['id'] != album_id]
        save_artist(artist)

# --- ファイル処理 ---
def allowed_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS_IMG

def allowed_audio(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS_AUDIO

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
    except Exception:
        if os.path.exists(temp_path): os.remove(temp_path)
        return None
    if os.path.exists(temp_path): os.remove(temp_path)
    return final_filename

# --- バックグラウンド処理 (YouTube DL) ---
def background_download_process(album_id, url, temp_track_id, start_track_num):
    try:
        ydl_opts_info = {'quiet': True, 'extract_flat': 'in_playlist', 'ignoreerrors': True}
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)
        if 'entries' in info: entries = list(info['entries'])
        else: entries = [info]

        album = load_album(album_id)
        if not album: return
        album['tracks'] = [t for t in album['tracks'] if t['id'] != temp_track_id]

        download_queue = []
        current_num = start_track_num
        for entry in entries:
            if not entry: continue
            track_id = str(uuid.uuid4())
            title = entry.get('title', 'Unknown Title')
            video_url = entry.get('url') or entry.get('webpage_url')
            placeholder = {
                "id": track_id, "title": f"【待機中】 {title}",
                "track_number": current_num, "filename": None,
                "processing": True, "original_url": video_url, "source_type": "youtube"
            }
            album['tracks'].append(placeholder)
            download_queue.append(placeholder)
            current_num += 1
        album['tracks'].sort(key=lambda x: x['track_number'])
        save_album(album)

        ydl_opts_dl = {
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '320'}],
            'quiet': True, 'ignoreerrors': True
        }

        for item in download_queue:
            album = load_album(album_id)
            if not album: break
            target_track = next((t for t in album['tracks'] if t['id'] == item['id']), None)
            if not target_track: continue
            
            target_track['title'] = f"【DL中...】 {item['title'].replace('【待機中】 ', '')}"
            save_album(album)
            try:
                base_id = uuid.uuid4().hex
                save_path_base = os.path.join(app.config['MUSIC_FOLDER'], base_id)
                current_opts = ydl_opts_dl.copy()
                current_opts['outtmpl'] = save_path_base
                with yt_dlp.YoutubeDL(current_opts) as ydl:
                    dl_info = ydl.extract_info(item['original_url'], download=True)
                    real_title = dl_info.get('title', 'Unknown Title')
                target_track['title'] = real_title
                target_track['filename'] = f"{base_id}.mp3"
                target_track.pop('processing', None)
                target_track.pop('error', None)
                save_album(album)
            except Exception:
                target_track['title'] = f"【エラー】 {item['title'].replace('【DL中...】 ', '').replace('【待機中】 ', '')}"
                target_track['processing'] = False
                target_track['error'] = True
                save_album(album)
    except Exception as e: print(f"YT BG Error: {e}")

# --- バックグラウンド処理 (Spotify DL) ---
def background_spotify_process(album_id, url, temp_track_id=None, start_track_num=1):
    try:
        # Spotifyクライアントの初期化（ID/Secretを渡す）
        spotdl_client = Spotdl(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)
        
        try:
            songs = spotdl_client.search([url])
        except Exception as e:
            print(f"Spotify Search Error: {e}")
            album = load_album(album_id)
            if album and temp_track_id:
                album['tracks'] = [t for t in album['tracks'] if t['id'] != temp_track_id]
                save_album(album)
            return

        album = load_album(album_id)
        if not album: return
        
        if temp_track_id:
            album['tracks'] = [t for t in album['tracks'] if t['id'] != temp_track_id]

        download_queue = []
        current_num = start_track_num

        for song in songs:
            track_id = str(uuid.uuid4())
            placeholder = {
                "id": track_id,
                "title": f"【待機中】 {song.name}",
                "track_number": current_num,
                "filename": None,
                "processing": True,
                "original_url": song.url,
                "source_type": "spotify"
            }
            album['tracks'].append(placeholder)
            download_queue.append(placeholder)
            current_num += 1
        
        album['tracks'].sort(key=lambda x: x['track_number'])
        save_album(album)

        for item in download_queue:
            album = load_album(album_id)
            if not album: break
            target = next((t for t in album['tracks'] if t['id'] == item['id']), None)
            if not target: continue

            target['title'] = f"【DL中...】 {item['title'].replace('【待機中】 ', '')}"
            save_album(album)

            try:
                base_id = uuid.uuid4().hex
                song_obj = next((s for s in songs if s.url == item['original_url']), None)
                if not song_obj: raise Exception("Song obj missing")

                os.chdir(app.config['UPLOAD_TEMP'])
                download_path = spotdl_client.download(song_obj)
                os.chdir(app.config['BASE_DIR'])

                if download_path:
                    src_path = os.path.join(app.config['UPLOAD_TEMP'], download_path)
                    dst_path = os.path.join(app.config['MUSIC_FOLDER'], f"{base_id}.mp3")
                    
                    # 320k固定で変換
                    subprocess.run(['ffmpeg', '-y', '-i', src_path, '-b:a', '320k', '-map', 'a', dst_path], 
                                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    if os.path.exists(src_path): os.remove(src_path)

                    target['title'] = song_obj.name
                    target['filename'] = f"{base_id}.mp3"
                    target.pop('processing', None)
                    target.pop('error', None)
                else:
                    raise Exception("Download returned None")
                
                save_album(album)

            except Exception as e:
                os.chdir(app.config['BASE_DIR'])
                target['title'] = f"【エラー】 {item['title'].replace('【DL中...】 ', '').replace('【待機中】 ', '')}"
                target['processing'] = False
                target['error'] = True
                save_album(album)
                print(f"SpotDL Error: {e}")

    except Exception as e:
        print(f"Spotify BG Error: {e}")
        os.chdir(app.config['BASE_DIR'])

# --- API ---
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
        if not track.get('processing') and not track.get('error') and track.get('filename'):
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
    new_artist = {
        "id": str(uuid.uuid4()), "name": request.form['name'],
        "genre": request.form.get('genre',''), "description": request.form.get('description',''),
        "image": img, "albums": []
    }
    save_artist(new_artist)
    return redirect(url_for('admin_index'))

@app.route('/admin/artist/<artist_id>/edit', methods=['POST'])
@requires_auth
def admin_edit_artist(artist_id):
    artist = load_artist(artist_id)
    if artist:
        artist['name'] = request.form['name']
        artist['genre'] = request.form['genre']
        artist['description'] = request.form['description']
        img = save_image_file(request.files.get('image'))
        if img: artist['image'] = img
        save_artist(artist)
    return redirect(url_for('admin_index'))

@app.route('/admin/artist/<artist_id>/delete', methods=['POST'])
@requires_auth
def admin_delete_artist(artist_id):
    delete_artist_data(artist_id)
    return redirect(url_for('admin_index'))

@app.route('/admin/artist/<artist_id>')
@requires_auth
def admin_view_artist(artist_id):
    artist = load_artist(artist_id)
    if not artist: return "Not found", 404
    return render_template('artist.html', artist=artist)

# --- アルバム自動作成機能 (Spotifyから) ---
@app.route('/admin/artist/<artist_id>/album/create_spotify', methods=['POST'])
@requires_auth
def admin_create_album_spotify(artist_id):
    url = request.form.get('url')
    if not url: return "URLが必要です", 400
    artist = load_artist(artist_id)
    if not artist: return "Artist not found", 404

    try:
        # Spotifyクライアント初期化（ここにも必要）
        client = Spotdl(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)
        songs = client.search([url])
        if not songs: return "曲が見つかりませんでした", 400
        
        first_song = songs[0]
        album_title = first_song.album_name or "Imported Album"
        album_year = first_song.year or ""
        
        album_id = str(uuid.uuid4())
        
        album_ref = {
            "id": album_id, "title": album_title, "year": str(album_year),
            "type": "Album", "cover_image": None
        }
        artist['albums'].append(album_ref)
        save_artist(artist)

        new_album_detail = {
            "id": album_id, "artist_id": artist_id, "artist_name": artist['name'],
            "title": album_title, "year": str(album_year),
            "type": "Album", "cover_image": None, "tracks": []
        }
        save_album(new_album_detail)

        thread = threading.Thread(
            target=background_spotify_process,
            args=(album_id, url, None, 1)
        )
        thread.start()

    except Exception as e:
        return f"Error: {e}", 500

    return redirect(url_for('admin_view_artist', artist_id=artist_id))

@app.route('/admin/artist/<artist_id>/album/add', methods=['POST'])
@requires_auth
def admin_add_album(artist_id):
    artist = load_artist(artist_id)
    if artist:
        album_id = str(uuid.uuid4())
        img = save_image_file(request.files.get('image'))
        album_ref = {
            "id": album_id, "title": request.form['title'],
            "year": request.form.get('year',''), "type": request.form.get('type','Album'),
            "cover_image": img
        }
        artist['albums'].append(album_ref)
        save_artist(artist)
        new_album = {
            "id": album_id, "artist_id": artist_id, "artist_name": artist['name'],
            "title": request.form['title'], "year": request.form.get('year',''),
            "type": request.form.get('type','Album'), "cover_image": img, "tracks": []
        }
        save_album(new_album)
    return redirect(url_for('admin_view_artist', artist_id=artist_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/edit', methods=['POST'])
@requires_auth
def admin_edit_album(artist_id, album_id):
    artist = load_artist(artist_id)
    album = load_album(album_id)
    if artist and album:
        title = request.form['title']
        year = request.form['year']
        atype = request.form['type']
        img = save_image_file(request.files.get('image'))
        for ref in artist['albums']:
            if ref['id'] == album_id:
                ref['title'] = title
                ref['year'] = year
                ref['type'] = atype
                if img: ref['cover_image'] = img
                break
        save_artist(artist)
        album['title'] = title
        album['year'] = year
        album['type'] = atype
        if img: album['cover_image'] = img
        save_album(album)
    return redirect(url_for('admin_view_artist', artist_id=artist_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/delete', methods=['POST'])
@requires_auth
def admin_delete_album(artist_id, album_id):
    delete_album_data(artist_id, album_id)
    return redirect(url_for('admin_view_artist', artist_id=artist_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>')
@requires_auth
def admin_view_album(artist_id, album_id):
    artist = load_artist(artist_id)
    album = load_album(album_id)
    if not artist or not album: return "Not found", 404
    return render_template('album.html', artist=artist, album=album)

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/add', methods=['POST'])
@requires_auth
def admin_add_track(artist_id, album_id):
    file = request.files.get('file')
    if not file or not file.filename: return "ファイルなし", 400
    fname = process_upload_file(file)
    if not fname: return "変換失敗", 500
    album = load_album(album_id)
    if album:
        num = request.form.get('track_number') or len(album['tracks'])+1
        new_track = {
            "id": str(uuid.uuid4()), "title": request.form.get('title') or file.filename,
            "track_number": int(num), "filename": fname
        }
        album['tracks'].append(new_track)
        album['tracks'].sort(key=lambda x: x['track_number'])
        save_album(album)
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/add_url', methods=['POST'])
@requires_auth
def admin_add_track_url(artist_id, album_id):
    url = request.form.get('url')
    if not url: return "URLなし", 400
    album = load_album(album_id)
    if not album: return "Not found", 404
    
    num = request.form.get('track_number') or len(album['tracks'])+1
    temp_id = str(uuid.uuid4())
    temp_track = {
        "id": temp_id, "title": "インポート準備中...",
        "track_number": int(num), "filename": None, "processing": True
    }
    album['tracks'].append(temp_track)
    album['tracks'].sort(key=lambda x: x['track_number'])
    save_album(album)

    if "spotify.com" in url:
        thread = threading.Thread(target=background_spotify_process, args=(album_id, url, temp_id, int(num)))
    else:
        thread = threading.Thread(target=background_download_process, args=(album_id, url, temp_id, int(num)))
    thread.start()
    
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/<track_id>/edit', methods=['POST'])
@requires_auth
def admin_edit_track(artist_id, album_id, track_id):
    album = load_album(album_id)
    if album:
        t = next((x for x in album['tracks'] if x['id']==track_id), None)
        if t:
            t['title'] = request.form['title']
            try: t['track_number'] = int(request.form['track_number'])
            except: pass
            album['tracks'].sort(key=lambda x: x['track_number'])
            save_album(album)
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/<track_id>/delete', methods=['POST'])
@requires_auth
def admin_delete_track(artist_id, album_id, track_id):
    album = load_album(album_id)
    if album:
        t = next((x for x in album['tracks'] if x['id']==track_id), None)
        if t and t.get('filename'):
            try: os.remove(os.path.join(app.config['MUSIC_FOLDER'], t['filename']))
            except: pass
        album['tracks'] = [x for x in album['tracks'] if x['id']!=track_id]
        save_album(album)
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/track/<track_id>/retry', methods=['POST'])
@requires_auth
def admin_retry_track(artist_id, album_id, track_id):
    album = load_album(album_id)
    if not album: return "Not found", 404
    t = next((x for x in album['tracks'] if x['id']==track_id), None)
    
    if t and t.get('original_url'):
        t['processing'] = True
        t.pop('error', None)
        t['title'] = f"【再試行中】 {t['title'].replace('【エラー】 ', '')}"
        save_album(album)
        
        source = t.get('source_type', 'youtube')
        if source == 'spotify':
             thread = threading.Thread(target=background_spotify_process, args=(album_id, t['original_url'], None, t['track_number']))
        else:
             thread = threading.Thread(target=background_download_process, args=(album_id, t['original_url'], None, t['track_number']))
        thread.start()

    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

@app.route('/admin/artist/<artist_id>/album/<album_id>/retry_all', methods=['POST'])
@requires_auth
def admin_retry_all(artist_id, album_id):
    album = load_album(album_id)
    if not album: return "Not found", 404
    
    for t in album['tracks']:
        if t.get('error') and t.get('original_url'):
            t['processing'] = True
            t.pop('error', None)
            t['title'] = f"【再試行中】 {t['title'].replace('【エラー】 ', '')}"
            
            source = t.get('source_type', 'youtube')
            if source == 'spotify':
                 thread = threading.Thread(target=background_spotify_process, args=(album_id, t['original_url'], None, t['track_number']))
            else:
                 thread = threading.Thread(target=background_download_process, args=(album_id, t['original_url'], None, t['track_number']))
            thread.start()
            time.sleep(0.5)

    save_album(album)
    return redirect(url_for('admin_view_album', artist_id=artist_id, album_id=album_id))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
