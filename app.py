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
from flask import Flask, request, jsonify, render_template, Response, stream_with_context, redirect, url_for, send_from_directory
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
# =============== ANÁLISE DE INVENTÁRIO ==================
# =========================================================

def analyze_inventory_report(codes_found: Set[str], caixas_csv_path: str) -> Dict[str, Any]:
    """
    Analisa códigos encontrados vs inventário registrado e gera relatório detalhado
    """
    try:
        # Ler todas as caixas registradas
        registered_boxes = read_registered_boxes(caixas_csv_path)
        
        # Converter códigos encontrados para lista para manipulação
        found_codes_list = list(codes_found)
        
        # Obter todos os códigos registrados (sem duplicatas)
        all_registered_codes = set()
        for box in registered_boxes:
            qr_codes = box.get('QRCodes', '').split('|') if box.get('QRCodes') else []
            barcodes = box.get('Barcodes', '').split('|') if box.get('Barcodes') else []
            all_registered_codes.update(qr_codes + barcodes)
        
        # Remover strings vazias
        all_registered_codes.discard('')
        
        # Análise de caixas
        boxes_complete = []  # Caixas com todos os códigos encontrados
        boxes_partial = []   # Caixas com alguns códigos encontrados
        boxes_missing = []   # Caixas sem nenhum código encontrado
        
        for box in registered_boxes:
            qr_codes = box.get('QRCodes', '').split('|') if box.get('QRCodes') else []
            barcodes = box.get('Barcodes', '').split('|') if box.get('Barcodes') else []
            box_codes = [c for c in (qr_codes + barcodes) if c.strip()]  # Remove vazios
            
            if not box_codes:  # Pular caixas sem códigos
                continue
                
            # Verificar quantos códigos da caixa foram encontrados
            found_in_box = [c for c in box_codes if c in codes_found]
            missing_in_box = [c for c in box_codes if c not in codes_found]
            
            if len(found_in_box) == len(box_codes) and len(found_in_box) > 0:
                # Caixa completa - todos os códigos encontrados
                boxes_complete.append({
                    'qrs': qr_codes,
                    'bcs': barcodes,
                    'codes': box_codes,
                    'codes_found': found_in_box
                })
            elif len(found_in_box) > 0:
                # Caixa parcial - alguns códigos encontrados
                boxes_partial.append({
                    'qrs': qr_codes,
                    'bcs': barcodes,
                    'codes': box_codes,
                    'codes_present': found_in_box,
                    'codes_missing': missing_in_box
                })
            else:
                # Caixa não encontrada - nenhum código encontrado
                boxes_missing.append({
                    'qrs': qr_codes,
                    'bcs': barcodes,
                    'codes': box_codes
                })
        
        # Códigos encontrados que não estão no inventário
        only_found = [c for c in found_codes_list if c not in all_registered_codes]
        
        # Códigos registrados que não foram encontrados
        only_registered = [c for c in all_registered_codes if c not in codes_found]
        
        # Construir relatório
        report = {
            'total_found': len(codes_found),
            'total_registered': len(all_registered_codes),
            'boxes_found': boxes_complete,
            'boxes_partial': boxes_partial,
            'boxes_missing': boxes_missing,
            'only_found': only_found,
            'only_registered': only_registered,
            'summary': {
                'boxes_complete_count': len(boxes_complete),
                'boxes_partial_count': len(boxes_partial),
                'boxes_missing_count': len(boxes_missing),
                'codes_not_in_inventory': len(only_found),
                'codes_not_found': len(only_registered)
            }
        }
        
        logger.info(f"Relatório gerado: {report['summary']}")
        return report
        
    except Exception as e:
        logger.error(f"Erro ao gerar relatório de inventário: {e}")
        return {
            'total_found': 0,
            'total_registered': 0,
            'boxes_found': [],
            'boxes_partial': [],
            'boxes_missing': [],
            'only_found': [],
            'only_registered': [],
            'summary': {
                'boxes_complete_count': 0,
                'boxes_partial_count': 0,
                'boxes_missing_count': 0,
                'codes_not_in_inventory': 0,
                'codes_not_found': 0
            },
            'error': str(e)
        }

# =========================================================
# ================= ROTAS FLASK ==========================
# =========================================================

@app.route('/')
def index():
    """Rota principal que redireciona para a câmera"""
    return redirect(url_for('camera'))

@app.route('/camera')
def camera():
    """Rota para a página da câmera"""
    return render_template('camera.html')

@app.route('/dashboard')
def dashboard():
    """Rota para o dashboard"""
    return render_template('dashboard.html')

@app.route('/ronda/comecar', methods=['POST'])
def ronda_comecar():
    """Inicia uma nova ronda de verificação"""
    try:
        upload_dir = app.config.get('UPLOAD_FOLDER', '.')
        csv_path = os.path.join(upload_dir, 'codigos_encontrados.csv')
        
        # Limpar arquivo de códigos encontrados para nova ronda
        clear_codigos_encontrados(csv_path)
        
        logger.info("Nova ronda iniciada")
        return jsonify({
            "status": "success", 
            "message": "Ronda iniciada com sucesso! Comece a escanear os códigos."
        })
    except Exception as e:
        logger.error(f"Erro ao iniciar ronda: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/ronda/encerrar', methods=['POST'])
def ronda_encerrar():
    """Encerra a ronda atual e gera relatório"""
    try:
        upload_dir = app.config.get('UPLOAD_FOLDER', '.')
        csv_path = os.path.join(upload_dir, 'codigos_encontrados.csv')
        caixas_path = os.path.join(upload_dir, 'caixas_registradas.csv')
        
        # Ler códigos encontrados
        codes_found = read_codigos_encontrados(csv_path)
        
        # Gerar relatório completo
        relatorio = analyze_inventory_report(codes_found, caixas_path)
        
        logger.info(f"Ronda encerrada. Relatório: {relatorio}")
        
        return jsonify({
            "status": "success",
            "message": "Ronda encerrada com sucesso! Relatório gerado.",
            **relatorio  # Incluir todos os dados do relatório
        })
    except Exception as e:
        logger.error(f"Erro ao encerrar ronda: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/ronda/status', methods=['GET'])
def ronda_status():
    """Retorna o status atual da ronda"""
    try:
        upload_dir = app.config.get('UPLOAD_FOLDER', '.')
        csv_path = os.path.join(upload_dir, 'codigos_encontrados.csv')
        caixas_path = os.path.join(upload_dir, 'caixas_registradas.csv')
        
        # Verificar se existe arquivo de códigos encontrados (indica ronda ativa)
        ronda_active = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
        
        # Ler códigos encontrados
        codes_found = read_codigos_encontrados(csv_path) if ronda_active else set()
        
        # Ler total de caixas registradas
        registered_boxes = read_registered_boxes(caixas_path) if os.path.exists(caixas_path) else []
        total_registered = len(registered_boxes)
        
        # Calcular estatísticas
        total_scanned = len(codes_found)
        
        # Calcular progresso (assumindo que cada caixa tem múltiplos códigos)
        progress = 0
        if total_registered > 0:
            found_boxes = 0
            for box in registered_boxes:
                box_codes = set()
                if box.get('QRCodes'):
                    box_codes.update(box['QRCodes'].split('|'))
                if box.get('Barcodes'):
                    box_codes.update(box['Barcodes'].split('|'))
                
                # Verificar se algum código da caixa foi encontrado
                if box_codes.intersection(codes_found):
                    found_boxes += 1
            
            progress = (found_boxes / total_registered) * 100
        
        status_message = f"Total escaneado: {total_scanned} códigos"
        if ronda_active:
            status_message += f" | Progresso: {progress:.1f}%"
        else:
            status_message = "Nenhuma ronda ativa"
            
        return jsonify({
            "status": "success",
            "ronda_active": ronda_active,
            "message": status_message,
            "progress": progress,
            "total_scanned": total_scanned,
            "found_items": total_scanned,  # Para compatibilidade com frontend
            "missing_items": max(0, total_registered - total_scanned),
            "total_registered": total_registered
        })
    except Exception as e:
        logger.error(f"Erro ao obter status da ronda: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/processar_video', methods=['POST'])
def processar_video():
    """Processa vídeo enviado para extrair códigos"""
    try:
        if 'video' not in request.files:
            return jsonify({"erro": "Nenhum arquivo de vídeo enviado"}), 400
        
        video_file = request.files['video']
        if video_file.filename == '':
            return jsonify({"erro": "Nome do arquivo vazio"}), 400
        
        # Salvar arquivo temporário
        upload_dir = app.config.get('UPLOAD_FOLDER', './temp_uploads')
        ensure_dir(upload_dir)
        
        filename = secure_filename(video_file.filename)
        temp_path = os.path.join(upload_dir, f"temp_{uuid.uuid4()}_{filename}")
        video_file.save(temp_path)
        
        try:
            # Extrair códigos do vídeo
            codes_found = extract_codes_from_video(temp_path)
            
            # Salvar códigos únicos
            csv_path = os.path.join(upload_dir, 'codigos_encontrados.csv')
            qr_codes = [code for code in codes_found if code.startswith('QR')]
            barcodes = [code for code in codes_found if not code.startswith('QR')]
            
            total_saved, duplicates = save_unique_codes(csv_path, qr_codes, barcodes)
            
            # Validar códigos
            validation_results = validate_codes_in_database(codes_found)
            success_count = sum(1 for result in validation_results if result['exists'])
            error_count = len(validation_results) - success_count
            
            return jsonify({
                "sucesso": True,
                "codes_found": len(codes_found),
                "total_saved": total_saved,
                "duplicates": len(duplicates),
                "success_count": success_count,
                "error_count": error_count,
                "results": validation_results
            })
            
        finally:
            # Limpar arquivo temporário
            try:
                os.remove(temp_path)
                logger.info(f"Arquivo temporário removido: {temp_path}")
            except Exception as e:
                logger.warning(f"Não foi possível remover arquivo temporário: {e}")
    
    except Exception as e:
        logger.error(f"Erro ao processar vídeo: {e}")
        return jsonify({"erro": f"Erro interno: {str(e)}"}), 500

# =========================================================
# =============== ROTAS PARA CÂMERA AO VIVO ==============
# =========================================================

@app.route('/camera/live/start', methods=['POST'])
def camera_live_start():
    """Inicia captura ao vivo da câmera"""
    try:
        data = request.get_json()
        camera_url = data.get('camera_url') if data else None
        
        if not camera_url:
            return jsonify({"ok": False, "msg": "URL da câmera não informada"}), 400
        
        # Aqui você implementaria a lógica de conexão com a câmera
        # Por enquanto, vamos simular sucesso
        logger.info(f"Iniciando captura da câmera: {camera_url}")
        
        return jsonify({
            "ok": True,
            "msg": f"Captura iniciada para {camera_url}"
        })
    except Exception as e:
        logger.error(f"Erro ao iniciar captura da câmera: {e}")
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.route('/camera/live/stop', methods=['POST'])
def camera_live_stop():
    """Para captura ao vivo da câmera"""
    try:
        logger.info("Parando captura da câmera")
        return jsonify({"ok": True, "msg": "Captura parada"})
    except Exception as e:
        logger.error(f"Erro ao parar captura da câmera: {e}")
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.route('/camera/live/poll', methods=['GET'])
def camera_live_poll():
    """Polling para detecções da câmera ao vivo"""
    try:
        # Simular algumas detecções para teste
        # Em uma implementação real, isso viria do sistema de captura
        detections = []
        
        return jsonify({
            "ok": True,
            "detections": detections
        })
    except Exception as e:
        logger.error(f"Erro no polling de detecções: {e}")
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.route('/camera/mjpeg')
def camera_mjpeg():
    """Stream MJPEG da câmera"""
    def generate():
        # Implementação placeholder - retorna um frame vazio
        yield b'--frame\r\n'
        yield b'Content-Type: image/jpeg\r\n\r\n'
        yield b'\r\n'
    
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/camera/frame.jpg')
def camera_frame():
    """Frame atual da câmera como JPEG"""
    try:
        # Implementação placeholder - retorna uma imagem pequena
        import io
        from PIL import Image
        
        # Criar uma imagem placeholder
        img = Image.new('RGB', (320, 240), color='gray')
        img_io = io.BytesIO()
        img.save(img_io, 'JPEG')
        img_io.seek(0)
        
        return Response(img_io.getvalue(), mimetype='image/jpeg')
    except Exception as e:
        logger.error(f"Erro ao gerar frame: {e}")
        return jsonify({"error": str(e)}), 500

# =========================================================
# =============== ROTAS PARA ARQUIVOS ESTÁTICOS ==========
# =========================================================

@app.route('/css/<path:filename>')
def serve_css(filename):
    """Serve arquivos CSS"""
    return send_from_directory('templates/css', filename)

@app.route('/js/<path:filename>')
def serve_js(filename):
    """Serve arquivos JavaScript"""
    return send_from_directory('templates/js', filename)

@app.route('/img/<path:filename>')
def serve_images(filename):
    """Serve arquivos de imagem"""
    return send_from_directory('templates/img', filename)

@app.route('/img/icons/<path:filename>')
def serve_icons(filename):
    """Serve ícones"""
    return send_from_directory('templates/img/icons', filename)

# =========================================================
# =============== ROTA PARA PÁGINA DE IMAGEM =============
# =========================================================

@app.route('/image', methods=['GET'])
def imagem_page():
    """
    Endpoint para servir a página de upload/captura de imagem
    """
    return render_template('image.html')

@app.errorhandler(413)
def too_large(e):
    return jsonify({"erro": "Arquivo muito grande. Tamanho máximo permitido excedido."}), 413

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"erro": "Erro interno do servidor"}), 500

if __name__ == '__main__':
    try:
        logger.info("Iniciando Olho de Águia Backend...")
        
        # Configurar diretório de upload
        upload_folder = os.path.join(os.getcwd(), 'temp_uploads')
        app.config['UPLOAD_FOLDER'] = upload_folder
        ensure_dir(upload_folder)
        
        # Inicializar banco de dados
        # init_database()
        
        # Configurar tamanho máximo de upload (100MB)
        app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
        
        # Iniciar servidor Flask
        app.run(host='0.0.0.0', port=5000, debug=True)
        
    except Exception as e:
        logger.error(f"Erro fatal ao iniciar aplicação: {e}")
        raise
