import os
import uuid
import time
import csv
import logging
import threading
from typing import Set, List, Dict, Any, Tuple, Optional

import cv2
import numpy as np
import requests
from requests.auth import HTTPDigestAuth, HTTPBasicAuth
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from flask_cors import CORS
from werkzeug.utils import secure_filename

from pyzbar import pyzbar
from pyzbar.pyzbar import decode

from sqlalchemy import create_engine, Column, Integer, String, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from urllib.parse import urlparse, urlunparse

# =========================================================
# =============== CONFIGURAÇÃO DE LOGGING =================
# =========================================================

def setup_logging() -> logging.Logger:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    root_logger = logging.getLogger()
    # Remove handlers antigos para evitar duplicação
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # synchronise werkzeug logs with same handler/level
    wkz = logging.getLogger("werkzeug")
    wkz.setLevel(level)
    for h in list(wkz.handlers):
        wkz.removeHandler(h)
    wkz.addHandler(handler)

    return logging.getLogger(__name__)

logger = setup_logging()

# =========================================================
# ===================== APP / CONFIG ======================
# =========================================================

app = Flask(__name__)
CORS(app)

app.config['UPLOAD_FOLDER'] = 'uploads/'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

# DB config (você informou que já tem o inventário)
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'aeroscan_db')
DB_USER = os.getenv('DB_USER', 'aeroscan_user')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'aeroscan_pass')

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

Base = declarative_base()
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    echo=os.getenv("LOG_SQL", "0") == "1"
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# =========================================================
# ======================= MODELOS =========================
# =========================================================

class Produto(Base):
    __tablename__ = "produtos"
    id = Column(Integer, primary_key=True, index=True)
    codigo_barra = Column(String, unique=True, index=True, nullable=False)
    nome_produto = Column(String, nullable=False)
    localizacao = Column(String, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo_barra': self.codigo_barra,
            'nome_produto': self.nome_produto,
            'localizacao': self.localizacao
        }

# =========================================================
# ================== VARIÁVEIS DE CAPTURA ==================
# =========================================================

_capture_thread: threading.Thread = None
_capture_lock = threading.Lock()
_capture_stop_event = threading.Event()
_capture_seen: Set[str] = set()           # evitar re-enfileiramento local repetido
_capture_queue: List[Dict[str, Any]] = [] # confirmações para o front
_queue_lock = threading.Lock()

# último frame JPEG (bytes) para preview no site + lock
_last_frame_jpeg: Optional[bytes] = None
_last_frame_lock = threading.Lock()

# =========================================================
# ================== FUNÇÕES AUXILIARES ===================
# =========================================================

def ensure_dir(path: str):
    dirn = os.path.dirname(path)
    if dirn:
        os.makedirs(dirn, exist_ok=True)

def clear_codigos_encontrados(csv_path: str):
    """
    Trunca/limpa o arquivo codigos_encontrados.csv (escreve apenas cabeçalho).
    """
    ensure_dir(csv_path)
    with open(csv_path, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['codigo', 'tipo'])
        writer.writeheader()
    logger.info(f"Arquivo {csv_path} limpo (ronda iniciada).")

def read_codigos_encontrados(csv_path: str) -> Set[str]:
    codes = set()
    if not os.path.exists(csv_path):
        return codes
    try:
        with open(csv_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                c = r.get('codigo')
                if c:
                    codes.add(c)
    except Exception as e:
        logger.exception(f"Erro ao ler {csv_path}: {e}")
    return codes

def read_all_registered_codes(caixas_csv_path: str) -> Set[str]:
    """
    Lê caixas_registradas.csv e retorna o conjunto de todos os códigos registrados (QRs + barcodes).
    """
    codes = set()
    if not os.path.exists(caixas_csv_path):
        return codes
    try:
        with open(caixas_csv_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                qrs = r.get('QRCodes', '')
                bcs = r.get('Barcodes', '')
                if qrs:
                    for x in qrs.split('|'):
                        if x:
                            codes.add(x)
                if bcs:
                    for x in bcs.split('|'):
                        if x:
                            codes.add(x)
    except Exception as e:
        logger.exception(f"Erro ao ler {caixas_csv_path}: {e}")
    return codes

def read_registered_boxes(caixas_csv_path: str) -> List[Dict[str, Any]]:
    """
    Lê caixas_registradas.csv e retorna lista de dicts:
    { 'qrs': [...], 'bcs': [...], 'codes': set([...]) }
    """
    boxes = []
    if not os.path.exists(caixas_csv_path):
        return boxes
    try:
        with open(caixas_csv_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                qrs = r.get('QRCodes', '')
                bcs = r.get('Barcodes', '')
                qr_list = [x for x in qrs.split('|') if x] if qrs else []
                bc_list = [x for x in bcs.split('|') if x] if bcs else []
                codes_set = set(qr_list + bc_list)
                boxes.append({
                    'qrs': qr_list,
                    'bcs': bc_list,
                    'codes': codes_set,
                    'raw_qrs': qrs,
                    'raw_bcs': bcs
                })
    except Exception as e:
        logger.exception(f"Erro ao ler caixas registradas: {e}")
    return boxes

# =========================================================
# ============ PROCESSAMENTO DE IMAGEM / VÍDEO ============
# =========================================================

def extract_codes_from_video(video_url: str, frame_skip: int = 10, backend=None) -> Set[str]:
    """
    Extrai códigos de um stream/arquivo/URL -- agora usa video_url como parâmetro.
    """
    codes_found: Set[str] = set()
    try:
        cap = cv2.VideoCapture(video_url, backend) if backend is not None else cv2.VideoCapture(video_url)
        if not cap or (hasattr(cap, "isOpened") and not cap.isOpened()):
            logger.error(f"Não foi possível abrir o vídeo: {video_url}")
            return codes_found
        frame_count = 0
        logger.info("Processando vídeo (extract_codes_from_video)...")
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frame_count += 1
            if frame_count % frame_skip != 0:
                continue
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            decoded_objects = pyzbar.decode(gray_frame)
            for obj in decoded_objects:
                code = obj.data.decode('utf-8')
                codes_found.add(code)
                logger.info(f"Código encontrado no frame {frame_count}: {code}")
        try:
            cap.release()
        except Exception:
            pass
        logger.info(f"Processamento concluído! Encontrados {len(codes_found)} códigos únicos.")
    except Exception as e:
        logger.exception(f"Erro ao processar vídeo: {e}")
    return codes_found

def validate_codes_in_database(codes: Set[str]) -> List[Dict[str, Any]]:
    results = []
    db = SessionLocal()
    try:
        for code in codes:
            produto = db.query(Produto).filter(Produto.codigo_barra == code).first()
            if produto:
                results.append({
                    "codigo": code,
                    "nome_produto": produto.nome_produto,
                    "localizacao": produto.localizacao,
                    "status": "✅ OK"
                })
            else:
                results.append({
                    "codigo": code,
                    "nome_produto": "Não encontrado",
                    "localizacao": "N/A",
                    "status": "❌ Erro"
                })
    except Exception as e:
        logger.exception(f"Erro ao validar códigos: {e}")
    finally:
        db.close()
    return results

# =========================================================
# ======== CSV: EVITAR DUPLICATAS / GRAVAR ACHADOS =========
# =========================================================

def load_existing_codes(csv_path: str) -> Dict[str, str]:
    existing = {}
    if not os.path.exists(csv_path):
        return existing
    try:
        with open(csv_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                codigo = row.get('codigo')
                tipo = row.get('tipo', '')
                if codigo:
                    existing[codigo] = tipo
    except Exception as e:
        logger.exception(f"Erro ao carregar códigos existentes: {e}")
    return existing

def rewrite_codes_file(csv_path: str, rows: List[Dict[str, str]]) -> None:
    dirn = os.path.dirname(csv_path)
    if dirn:
        os.makedirs(dirn, exist_ok=True)
    with open(csv_path, mode='w', newline='', encoding='utf-8') as f:
        fieldnames = ['codigo', 'tipo']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

def save_unique_codes(csv_path: str, qr_codes: List[str], barcodes: List[str]) -> Tuple[int, List[str]]:
    """
    Salva códigos detectados em codigos_encontrados.csv evitando duplicatas no arquivo.
    Retorna (num_novos, lista_novos)
    """
    os.makedirs(os.path.dirname(csv_path) or '.', exist_ok=True)
    existing = load_existing_codes(csv_path)

    merged: Dict[str, str] = {}
    for q in qr_codes:
        if q:
            merged[q] = 'QR Code'
    for b in barcodes:
        if b and b not in merged:
            merged[b] = 'Barcode'

    to_add: Dict[str, str] = {}
    for codigo, tipo in merged.items():
        if codigo not in existing:
            to_add[codigo] = tipo

    new_list: List[str] = []
    if to_add:
        file_exists = os.path.isfile(csv_path)
        try:
            with open(csv_path, mode='a', newline='', encoding='utf-8') as f:
                fieldnames = ['codigo', 'tipo']
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if not file_exists or os.stat(csv_path).st_size == 0:
                    writer.writeheader()
                for codigo, tipo in to_add.items():
                    writer.writerow({'codigo': codigo, 'tipo': tipo})
                    new_list.append(codigo)
            logger.info(f"Gravados {len(new_list)} novo(s) código(s) em {csv_path}.")
        except Exception as e:
            logger.exception(f"Erro ao gravar novos códigos: {e}")
    return len(new_list), new_list

# =========================================================
# =========== REGISTRAR/VERIFICAR CAIXAS ==================
# =========================================================

def caixa_exists(caixas_csv_path: str, qr_codes: List[str], barcodes: List[str]) -> bool:
    if not os.path.exists(caixas_csv_path):
        return False
    try:
        nova_qrs = tuple(sorted(qr_codes))
        nova_bcs = tuple(sorted(barcodes))
        with open(caixas_csv_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                qrs = row.get('QRCodes', '')
                bcs = row.get('Barcodes', '')
                qr_list = tuple(sorted(qrs.split('|'))) if qrs else tuple()
                bc_list = tuple(sorted(bcs.split('|'))) if bcs else tuple()
                if qr_list == nova_qrs and bc_list == nova_bcs:
                    logger.info("Combinação de códigos já registrada anteriormente.")
                    return True
    except Exception as e:
        logger.exception(f"Erro ao verificar caixas existentes: {e}")
    return False

def register_caixa(caixas_csv_path: str, contagem_path: str, qr_codes: List[str], barcodes: List[str]) -> Tuple[bool, int]:
    os.makedirs(os.path.dirname(caixas_csv_path) or '.', exist_ok=True)
    nova_qrs = tuple(sorted(qr_codes))
    nova_bcs = tuple(sorted(barcodes))
    if caixa_exists(caixas_csv_path, list(nova_qrs), list(nova_bcs)):
        total = 0
        if os.path.exists(contagem_path):
            try:
                with open(contagem_path, 'r', encoding='utf-8') as f:
                    total = int(f.read().strip() or '0')
            except Exception:
                total = 0
        logger.info("Caixa já registrada; contagem mantida.")
        return False, total
    file_exists = os.path.isfile(caixas_csv_path)
    try:
        with open(caixas_csv_path, mode='a', newline='', encoding='utf-8') as f:
            fieldnames = ['QRCodes', 'Barcodes']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists or os.stat(caixas_csv_path).st_size == 0:
                writer.writeheader()
            writer.writerow({
                'QRCodes': '|'.join(nova_qrs),
                'Barcodes': '|'.join(nova_bcs)
            })
        logger.info("Nova caixa registrada com sucesso.")
    except Exception as e:
        logger.exception(f"Erro ao gravar caixa em {caixas_csv_path}: {e}")
        return False, 0
    total = 0
    if os.path.exists(contagem_path):
        try:
            with open(contagem_path, 'r', encoding='utf-8') as f:
                total = int(f.read().strip() or '0')
        except Exception:
            total = 0
    total += 1
    try:
        with open(contagem_path, 'w', encoding='utf-8') as f:
            f.write(str(total))
        logger.info(f"Contagem de caixas atualizada: {total}.")
    except Exception as e:
        logger.exception(f"Erro ao atualizar contagem em {contagem_path}: {e}")
    return True, total

# =========================================================
# =========== VERIFICAÇÃO DE EXISTÊNCIA DE UMA CAIXA ======
# =========================================================

def verificar_caixa(codigo: str) -> str:
    """
    Retorna "ok" se o código estiver presente em qualquer caixa registrada,
    caso contrário "erro". Também imprime "ok"/"erro" no console conforme seu contrato.
    """
    try:
        upload_dir = app.config.get('UPLOAD_FOLDER', '.')
        caixas_csv = os.path.join(upload_dir, 'caixas_registradas.csv')
        if not os.path.exists(caixas_csv):
            logger.info(f"Arquivo de caixas não encontrado ao verificar código {codigo}")
            print("erro")
            return "erro"
        with open(caixas_csv, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                qr_codes = row.get('QRCodes', '').split('|') if row.get('QRCodes') else []
                barcodes = row.get('Barcodes', '').split('|') if row.get('Barcodes') else []
                if codigo in qr_codes or codigo in barcodes:
                    logger.info(f"Caixa encontrada com o código {codigo}")
                    print("ok")
                    return "ok"
        logger.info(f"Nenhuma caixa encontrada com o código {codigo}")
        print("erro")
        return "erro"
    except Exception as e:
        logger.exception(f"Erro ao verificar caixa {codigo}: {str(e)}")
        print("erro")
        return "erro"

# =========================================================
# ============== CAPTURA AO VIVO (IP CAMERA) =============
# =========================================================

def _get_frame_from_http_snapshot(url: str, timeout: float = 5.0, auth: Optional[Any] = None):
    """Tenta obter um JPG estático via HTTP (snapshot endpoint). Retorna frame BGR ou None."""
    try:
        resp = requests.get(url, timeout=timeout, auth=auth, verify=False)
        if resp.status_code != 200:
            logger.debug(f"Snapshot HTTP retornou status {resp.status_code} para {url}")
            return None
        arr = np.frombuffer(resp.content, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img
    except Exception as e:
        logger.debug(f"Erro ao obter snapshot HTTP: {e}")
        return None

def _mjpeg_frame_generator(url: str, auth: Optional[Any] = None, timeout: float = 10.0):
    """
    Generator que consome um MJPEG HTTP stream e retorna frames (BGR numpy arrays).
    Usa parsing simples procurando pelos marcadores JPEG (0xFFD8 ... 0xFFD9).
    Útil quando cv2.VideoCapture falha em abrir MJPEGs servidos por alguns apps de celular.
    """
    try:
        with requests.get(url, stream=True, timeout=timeout, auth=auth, verify=False) as r:
            if r.status_code != 200:
                logger.debug(f"MJPEG stream retornou status {r.status_code} para {url}")
                return
            buf = bytes()
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    buf += chunk
                    # procurar começo e fim JPEG
                    start = buf.find(b'\xff\xd8')
                    end = buf.find(b'\xff\xd9')
                    if start != -1 and end != -1 and end > start:
                        jpg = buf[start:end+2]
                        buf = buf[end+2:]
                        arr = np.frombuffer(jpg, dtype=np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if img is not None:
                            yield img
    except Exception as e:
        logger.debug(f"Erro ao consumir MJPEG: {e}")
        return

def _try_opencv_capture_once(url: str, backend=None, timeout_sec: float = 5.0) -> bool:
    """Tenta abrir VideoCapture e ler um frame (apenas para teste). Retorna True se ok."""
    cap = None
    start = time.time()
    try:
        if backend is not None:
            cap = cv2.VideoCapture(url, backend)
        else:
            cap = cv2.VideoCapture(url)
        if not cap or (hasattr(cap, "isOpened") and not cap.isOpened()):
            logger.debug(f"cv2.VideoCapture não abriu (backend={backend}) para {url}")
            return False
        while time.time() - start < timeout_sec:
            ret, frame = cap.read()
            if ret and frame is not None:
                return True
            time.sleep(0.2)
        return False
    except Exception as e:
        logger.debug(f"Exceção em _try_opencv_capture_once: {e}")
        return False
    finally:
        try:
            if cap:
                cap.release()
        except Exception:
            pass

def _capture_loop(camera_url: str, mode: str = 'opencv', read_interval: float = 0.25, auth: Optional[Any] = None):
    """
    Loop de captura em background.
    Quando detecta códigos novos, grava em codigos_encontrados.csv (apenas novos) e
    adiciona confirmação na fila para o front.
    mode: 'opencv' | 'snapshot' | 'mjpeg'
    auth: requests auth object or None
    """
    global _capture_seen, _last_frame_jpeg
    logger.info(f"Thread de captura iniciada para {camera_url} (mode={mode})")
    cap = None
    mjpeg_gen = None
    try:
        if mode == 'opencv':
            cap = cv2.VideoCapture(camera_url)
            if not cap.isOpened():
                logger.error("VideoCapture não abriu no loop (modo opencv). Encerrando thread.")
                return
        elif mode == 'mjpeg':
            mjpeg_gen = _mjpeg_frame_generator(camera_url, auth=auth, timeout=10.0)

        while not _capture_stop_event.is_set():
            frame = None
            if mode == 'opencv':
                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.5)
                    continue
            elif mode == 'snapshot':
                frame = _get_frame_from_http_snapshot(camera_url, timeout=3.0, auth=auth)
                if frame is None:
                    time.sleep(0.5)
                    continue
            elif mode == 'mjpeg':
                try:
                    frame = next(mjpeg_gen)
                except StopIteration:
                    logger.debug("MJPEG generator terminou.")
                    break
                except Exception as e:
                    logger.debug(f"Erro lendo MJPEG generator: {e}")
                    time.sleep(0.5)
                    continue

            # atualiza último frame JPEG para preview no site
            try:
                ok_enc, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ok_enc:
                    with _last_frame_lock:
                        _last_frame_jpeg = buf.tobytes()
            except Exception as e:
                logger.debug(f"Falha ao codificar frame para JPEG: {e}")

            try:
                decoded = pyzbar.decode(frame)
            except Exception as e:
                logger.debug(f"pyzbar.decode falhou: {e}")
                decoded = []

            frame_codes = []
            for obj in decoded:
                try:
                    code = obj.data.decode('utf-8')
                except Exception:
                    code = None
                if code and code not in frame_codes:
                    frame_codes.append(code)

            if not frame_codes:
                time.sleep(read_interval)
                continue

            # Se o frame contiver >=2 códigos, tentar registrar como caixa também
            upload_dir = app.config.get('UPLOAD_FOLDER', '.')
            codigos_csv = os.path.join(upload_dir, 'codigos_encontrados.csv')
            caixas_csv = os.path.join(upload_dir, 'caixas_registradas.csv')
            contagem_txt = os.path.join(upload_dir, 'contagem_caixas.txt')

            # gravar códigos novos no arquivo global (apenas novos)
            qr_codes = []
            barcodes = []
            # classificar por tipo usando decoded objects
            for obj in decoded:
                try:
                    code = obj.data.decode('utf-8')
                except Exception:
                    continue
                if obj.type == 'QRCODE':
                    qr_codes.append(code)
                else:
                    barcodes.append(code)

            num_new, new_list = save_unique_codes(codigos_csv, qr_codes, barcodes)
            if num_new > 0:
                # enfileira confirmações apenas para códigos novos
                for c in new_list:
                    if c in _capture_seen:
                        continue
                    confirmation = {
                        'codigo': c,
                        'tipo': 'QR Code' if c in qr_codes else 'Barcode',
                        'timestamp': time.time()
                    }
                    with _queue_lock:
                        _capture_queue.append(confirmation)
                    logger.info(f"Código novo detectado e gravado: {c}")
                    _capture_seen.add(c)
            else:
                logger.debug("Nenhum código novo gravado deste frame.")

            # se frame possui >=2 códigos, tentar registrar caixa (comportamento solicitado)
            unique_frame_codes = sorted(set(frame_codes))
            if len(unique_frame_codes) >= 2:
                qr_for_box = [c for c in unique_frame_codes if c in qr_codes]
                bc_for_box = [c for c in unique_frame_codes if c in barcodes]
                # registrar caixa (pode já existir)
                foi_reg, total_caixas = register_caixa(caixas_csv, contagem_txt, qr_for_box, bc_for_box)
                if foi_reg:
                    logger.info("Caixa registrada a partir do frame com múltiplos códigos.")
                    with _queue_lock:
                        _capture_queue.append({
                            'caixa': True,
                            'codes': unique_frame_codes,
                            'timestamp': time.time()
                        })
                else:
                    logger.debug("Caixa do frame já estava registrada anteriormente.")

            time.sleep(read_interval)

    except Exception as e:
        logger.exception(f"Erro na thread de captura: {e}")
    finally:
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass
        logger.info("Thread de captura finalizada.")

# ================== End capture helpers ==================

def _build_auth_for_url(camera_url: str, username: Optional[str], password: Optional[str]):
    """
    Se username/password estiverem presentes, retorna objeto de auth para requests.
    Tenta detectar se Digest é necessário? Não há detecção 100% confiável — deixamos o caller optar.
    Aqui devolvemos HTTPBasicAuth por padrão; camera_live_start pode trocar para Digest se desejado.
    """
    if not username:
        return None
    # default to basic auth, user can request digest by sending "auth_type" field (handled below)
    return HTTPBasicAuth(username, password or '')

@app.route('/camera/frame.jpg')
def camera_frame():
    """
    Retorna o último frame como JPEG (snapshot). Usado pelo frontend como fallback.
    """
    with _last_frame_lock:
        data = _last_frame_jpeg
    if not data:
        # retornar 204 No Content para indicar que ainda não há frame
        return Response(status=204)
    headers = {
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0'
    }
    return Response(data, mimetype='image/jpeg', headers=headers)

@app.route('/camera/mjpeg')
def camera_mjpeg():
    """
    Serve um MJPEG utilizando o último frame disponível (multipart).
    Navegadores compatíveis conseguem exibir o <img src="/camera/mjpeg"> diretamente.
    """
    def generator():
        while True:
            with _last_frame_lock:
                frame = _last_frame_jpeg
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.1)
    return Response(stream_with_context(generator()), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/camera/live/start', methods=['POST'])
def camera_live_start():
    """
    Inicia captura contínua a partir de camera_url (RTSP/HTTP snapshot/MJPEG).
    Recebe JSON { "camera_url": "rtsp://...", "username": "...", "password": "...", "auth_type": "digest" }
    ou via form fields.
    Retorna JSON com 'ok' bool e 'msg' e 'mode' usado.
    """
    global _capture_thread, _capture_stop_event, _capture_seen
    camera_url = None
    username = None
    password = None
    auth_type = None
    if request.is_json:
        json_body = request.get_json()
        camera_url = json_body.get('camera_url')
        username = json_body.get('username') or None
        password = json_body.get('password') or None
        auth_type = json_body.get('auth_type') or None
    else:
        camera_url = request.form.get('camera_url') or request.values.get('camera_url')
        username = request.form.get('username') or None
        password = request.form.get('password') or None
        auth_type = request.form.get('auth_type') or None

    camera_url = camera_url or os.getenv('CAMERA_URL')
    if not camera_url:
        return jsonify({'ok': False, 'msg': 'camera_url ausente'}), 400

    with _capture_lock:
        if _capture_thread and _capture_thread.is_alive():
            logger.info("Captura já em execução; retornando sucesso.")
            return jsonify({'ok': True, 'msg': 'já em execução', 'mode': 'existing'})

    # preparar objeto de auth para requests se necessário
    requests_auth = None
    if username:
        if (auth_type or '').lower() == 'digest':
            requests_auth = HTTPDigestAuth(username, password or '')
        else:
            requests_auth = HTTPBasicAuth(username, password or '')

    # Testar OpenCV backends
    logger.info(f"Tentando conectar via OpenCV a {camera_url}")
    ok = False
    chosen_mode = None
    try:
        if hasattr(cv2, "CAP_FFMPEG"):
            ok = _try_opencv_capture_once(camera_url, backend=cv2.CAP_FFMPEG, timeout_sec=4.0)
            logger.debug(f"Teste OpenCV CAP_FFMPEG -> {ok}")
        if not ok and hasattr(cv2, "CAP_GSTREAMER"):
            ok = _try_opencv_capture_once(camera_url, backend=cv2.CAP_GSTREAMER, timeout_sec=4.0)
            logger.debug(f"Teste OpenCV CAP_GSTREAMER -> {ok}")
        if not ok:
            ok = _try_opencv_capture_once(camera_url, backend=None, timeout_sec=4.0)
            logger.debug(f"Teste OpenCV generic -> {ok}")
    except Exception as e:
        logger.debug(f"Teste OpenCV gerou exceção: {e}")
        ok = False

    if ok:
        chosen_mode = 'opencv'
    else:
        # Se URL começa com http(s), testar snapshot e MJPEG
        if camera_url.lower().startswith('http'):
            logger.info("OpenCV falhou; tentando snapshot HTTP como fallback.")
            # tentar paths comuns caso URL seja a base do servidor de câmera
            snapshot_candidates = [camera_url]
            snapshot_candidates += [
                camera_url.rstrip('/') + '/shot.jpg',
                camera_url.rstrip('/') + '/photo.jpg',
                camera_url.rstrip('/') + '/snapshot.jpg',
                camera_url.rstrip('/') + '/img.jpg',
                camera_url.rstrip('/') + '/video'
            ]
            snap_ok = False
            for s in snapshot_candidates:
                img = _get_frame_from_http_snapshot(s, timeout=3.0, auth=requests_auth)
                if img is not None:
                    camera_url = s  # usar o endpoint que funcionou
                    chosen_mode = 'snapshot'
                    snap_ok = True
                    ok = True
                    logger.info(f"Snapshot HTTP OK em {s} — usaremos modo 'snapshot'.")
                    break

            if not snap_ok:
                # tentar MJPEG parser (caso VideoCapture não abra)
                logger.info("Snapshot falhou; tentando consumir MJPEG (parsing).")
                try:
                    gen = _mjpeg_frame_generator(camera_url, auth=requests_auth, timeout=5.0)
                    first = None
                    if gen is not None:
                        try:
                            first = next(gen)
                        except Exception:
                            first = None
                    if first is not None:
                        chosen_mode = 'mjpeg'
                        ok = True
                        logger.info("MJPEG parser OK — usaremos modo 'mjpeg'.")
                except Exception as e:
                    logger.debug(f"MJPEG parser também falhou: {e}")

    if not ok:
        msg = ("Não foi possível abrir stream via OpenCV nem via snapshot/MJPEG HTTP. "
               "Verifique URL, autenticação, rede (Docker/VM host network?), e se a câmera exige digest auth.")
        logger.warning(msg + f" camera_url={camera_url}")
        return jsonify({'ok': False, 'msg': msg}), 502

    # start capture thread
    with _capture_lock:
        _capture_stop_event.clear()
        _capture_seen = set()
        with _queue_lock:
            _capture_queue.clear()
        # pass auth object to thread for snapshot/mjpeg modes
        _capture_thread = threading.Thread(target=_capture_loop, args=(camera_url, chosen_mode, 0.25, requests_auth), daemon=True)
        _capture_thread.start()

    logger.info("Captura contínua iniciada (background).")
    return jsonify({'ok': True, 'msg': f'conectado (mode={chosen_mode})', 'mode': chosen_mode})

@app.route('/camera/live/stop', methods=['POST'])
def camera_live_stop():
    global _capture_thread, _capture_stop_event
    with _capture_lock:
        if not _capture_thread or not _capture_thread.is_alive():
            return jsonify({'ok': True, 'msg': 'nenhuma captura em execução'})
        _capture_stop_event.set()
        _capture_thread.join(timeout=5.0)
        _capture_thread = None
        # limpar último frame quando parar
        global _last_frame_jpeg
        with _last_frame_lock:
            _last_frame_jpeg = None
    logger.info("Captura contínua parada.")
    return jsonify({'ok': True, 'msg': 'captura parada'})

@app.route('/camera/live/poll', methods=['GET'])
def camera_live_poll():
    items = []
    with _queue_lock:
        items = list(_capture_queue)
        _capture_queue.clear()
    return jsonify({'ok': True, 'detections': items})

# =========================================================
# ======================== ROTAS ==========================
# =========================================================

@app.route('/camera')
def camera_page():
    return render_template('image.html')

@app.route('/image', methods=['GET'])
def imagem_page():
    return render_template('image.html')

@app.route('/ronda/comecar', methods=['POST'])
def ronda_comecar():
    upload_dir = app.config.get('UPLOAD_FOLDER', '.')
    codigos_csv = os.path.join(upload_dir, 'codigos_encontrados.csv')
    try:
        clear_codigos_encontrados(codigos_csv)
        mensagem = "Ronda iniciada: codigos_encontrados.csv limpo."
        logger.info(mensagem)
        # Se a requisição for AJAX/JSON, devolve JSON para evitar reload (mantendo a câmera rodando)
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json'] > request.accept_mimetypes['text/html']:
            return jsonify({'ok': True, 'msg': mensagem})
        return render_template('image.html', mensagens=[mensagem])
    except Exception as e:
        logger.exception("Erro ao iniciar ronda")
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': False, 'error': str(e)}), 500
        return render_template('image.html', error=f"Erro ao iniciar ronda: {e}")

@app.route('/ronda/encerrar', methods=['POST'])
def ronda_encerrar():
    upload_dir = app.config.get('UPLOAD_FOLDER', '.')
    codigos_csv = os.path.join(upload_dir, 'codigos_encontrados.csv')
    caixas_csv = os.path.join(upload_dir, 'caixas_registradas.csv')

    found_codes = read_codigos_encontrados(codigos_csv)
    registered_codes = read_all_registered_codes(caixas_csv)
    registered_boxes = read_registered_boxes(caixas_csv)

    only_found = sorted(list(found_codes - registered_codes))
    only_registered = sorted(list(registered_codes - found_codes))
    common = sorted(list(found_codes & registered_codes))

    boxes_found = []
    boxes_partial = []
    boxes_missing = []
    for box in registered_boxes:
        box_codes = box['codes']
        if box_codes and box_codes.issubset(found_codes):
            boxes_found.append({
                'qrs': box['qrs'],
                'bcs': box['bcs'],
                'codes': sorted(list(box_codes))
            })
        elif box_codes and (box_codes & found_codes):
            boxes_partial.append({
                'qrs': box['qrs'],
                'bcs': box['bcs'],
                'codes_present': sorted(list(box_codes & found_codes)),
                'codes_missing': sorted(list(box_codes - found_codes))
            })
        else:
            boxes_missing.append({
                'qrs': box['qrs'],
                'bcs': box['bcs'],
                'codes': sorted(list(box_codes))
            })

    relatorio = {
        "total_found": len(found_codes),
        "total_registered": len(registered_codes),
        "only_found": only_found,
        "only_registered": only_registered,
        "common": common,
        "boxes_found": boxes_found,
        "boxes_partial": boxes_partial,
        "boxes_missing": boxes_missing
    }

    logger.info(f"Ronda encerrada. Encontrados {len(found_codes)} códigos; Registrados {len(registered_codes)}.")

    # Se chamada via AJAX/JSON, retorna JSON (evita reload)
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes['application/json'] > request.accept_mimetypes['text/html']:
        return jsonify({'ok': True, 'relatorio': relatorio, 'mensagens': [f"Ronda encerrada. Ver relatório."]})
    return render_template('image.html', relatorio=relatorio, mensagens=[f"Ronda encerrada. Ver relatório."])

@app.route('/verificar_caixa', methods=['POST'])
def verificar_caixa_endpoint():
    codigo = request.form.get('codigo')
    if not codigo:
        return render_template('image.html', mensagem_verificacao="Por favor, insira um código para verificar.")
    resultado = verificar_caixa(codigo)
    if resultado == "ok":
        mensagem = f"✅ Caixa com código {codigo} foi encontrada!"
    else:
        mensagem = f"❌ Caixa com código {codigo} não foi encontrada."
    return render_template('image.html', mensagem_verificacao=mensagem, codigo_verificado=codigo)

@app.route('/image/upload', methods=['POST'])
def upload_image():
    """
    Upload de imagem: detecta códigos, verifica se estão cadastrados em caixas registradas
    grava códigos em codigos_encontrados.csv (apenas novos) e se imagem tem >=2 códigos registra caixa.
    """
    if 'image' not in request.files:
        return render_template('image.html', error='Nenhum arquivo enviado.')
    file = request.files['image']
    if file.filename == '':
        return render_template('image.html', error='Nome de arquivo vazio.')
    try:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{int(time.time())}_{filename}")
        file.save(filepath)
        logger.info(f"Imagem recebida e salva em: {filepath}")

        image = cv2.imread(filepath)
        if image is None:
            try:
                os.remove(filepath)
            except Exception:
                pass
            return render_template('image.html', error='Erro ao ler a imagem.')

        upload_dir = app.config.get('UPLOAD_FOLDER', '.')
        detected = decode(image)
        qr_codes = [d.data.decode('utf-8') for d in detected if d.type == 'QRCODE']
        barcodes = [d.data.decode('utf-8') for d in detected if d.type != 'QRCODE']

        logger.info(f"Detectados {len(qr_codes)} QR(s) e {len(barcodes)} barcode(s).")

        status_codigos: List[Dict[str, str]] = []
        todos_codigos = qr_codes + barcodes
        for codigo in todos_codigos:
            resultado = verificar_caixa(codigo)
            if resultado == "ok":
                status = "✅ Este código já estava cadastrado no sistema."
            else:
                status = "❌ Este código NÃO estava cadastrado no sistema."
            status_codigos.append({"codigo": codigo, "status": status})

        if not todos_codigos:
            try:
                os.remove(filepath)
            except Exception:
                pass
            return render_template('image.html', error='Nenhum código encontrado na imagem.', status_codigos=status_codigos)

        codigos_csv = os.path.join(upload_dir, 'codigos_encontrados.csv')
        num_new_codes, new_codes_list = save_unique_codes(codigos_csv, qr_codes, barcodes)
        mensagens = []
        if num_new_codes > 0:
            mensagens.append(f"{num_new_codes} código(s) novos adicionados em codigos_encontrados.csv.")
        else:
            mensagens.append("Nenhum código novo para adicionar ao codigos_encontrados.csv.")

        # Se a imagem tem >=2 códigos, registrar como caixa
        caixas_csv = os.path.join(upload_dir, 'caixas_registradas.csv')
        contagem_txt = os.path.join(upload_dir, 'contagem_caixas.txt')
        foi_registrada_caixa = False
        total_caixas = 0
        if len(set(todos_codigos)) >= 2:
            qr_novos = [c for c in qr_codes]
            barcode_novos = [c for c in barcodes]
            foi_registrada_caixa, total_caixas = register_caixa(caixas_csv, contagem_txt, qr_novos, barcode_novos)
            if foi_registrada_caixa:
                mensagens.append("Imagem continha >=2 códigos — caixa registrada com sucesso.")
            else:
                mensagens.append("Imagem continha >=2 códigos — caixa já estava registrada anteriormente.")

        try:
            os.remove(filepath)
            logger.debug(f"Imagem temporária removida: {filepath}")
        except Exception:
            pass

        mensagens.append("Observação: na verificação individual, códigos NÃO registrados são apontados como erro.")
        return render_template('image.html',
                               qr_codes=qr_codes,
                               barcodes=barcodes,
                               mensagens=mensagens,
                               status_codigos=status_codigos,
                               total_caixas=total_caixas)
    except Exception as e:
        logger.exception("Erro ao processar upload de imagem")
        return render_template('image.html', error=f'Erro ao processar a imagem: {str(e)}')

# =========================================================
# =================== ENDPOINTS UTILES ====================
# =========================================================

@app.route('/api/health', methods=['GET'])
def health_check():
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        db_status = "OK"
    except Exception as e:
        db_status = f"Erro: {str(e)}"
    return jsonify({
        "status": "OK",
        "database": db_status,
        "message": "Olho de Águia Backend está funcionando!"
    })

@app.route('/api/produtos', methods=['GET'])
def listar_produtos():
    db = SessionLocal()
    try:
        produtos = db.query(Produto).all()
        return jsonify({
            "produtos": [produto.to_dict() for produto in produtos],
            "total": len(produtos)
        })
    except Exception as e:
        logger.exception(f"Erro ao listar produtos: {e}")
        return jsonify({"erro": str(e)}), 500
    finally:
        db.close()

# =========================================================
# =================== HANDLERS DE ERRO ====================
# =========================================================

@app.errorhandler(413)
def too_large(e):
    logger.warning("Arquivo muito grande enviado ao servidor.")
    return jsonify({"erro": "Arquivo muito grande. Tamanho máximo permitido excedido."}), 413

@app.errorhandler(500)
def internal_error(e):
    logger.exception("Erro interno do servidor")
    return jsonify({"erro": "Erro interno do servidor"}), 500

# =========================================================
# ======================= STARTUP =========================
# =========================================================

if __name__ == '__main__':
    try:
        logger.info("Iniciando Olho de Águia Backend...")
        # NOTA: removida inicialização/criação do inventário (você informou que já possui)
        app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB
        app.run(host='0.0.0.0', port=5000, debug=True)
    except Exception as e:
        logger.exception(f"Erro fatal ao iniciar aplicação: {e}")
        raise
