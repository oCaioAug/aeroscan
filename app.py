import os
import uuid
import time
import csv
import logging
from typing import Set, List, Dict, Any, Tuple

import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from werkzeug.utils import secure_filename

from pyzbar import pyzbar
from pyzbar.pyzbar import decode

from sqlalchemy import create_engine, Column, Integer, String, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# =========================================================
# =============== CONFIGURAÇÃO DE LOGGING =================
# =========================================================

def setup_logging() -> logging.Logger:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    root_logger = logging.getLogger()
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

# DB config (você disse que já tem o inventário)
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
# ================== MIDDLEWARE DE LOG ====================
# =========================================================

@app.before_request
def _log_request_start():
    request._req_id = uuid.uuid4().hex[:8]
    request._start_time = time.perf_counter()
    logger.info(
        f"[REQ {request._req_id}] {request.method} {request.path} | "
        f"IP={request.headers.get('X-Forwarded-For', request.remote_addr)} | "
        f"UA={request.headers.get('User-Agent', '-')}"
    )

@app.after_request
def _log_request_end(response):
    try:
        dur_ms = (time.perf_counter() - getattr(request, "_start_time", time.perf_counter())) * 1000.0
        logger.info(
            f"[REQ {getattr(request, '_req_id', '????')}] {request.method} {request.path} "
            f"-> {response.status_code} | {dur_ms:.1f} ms"
        )
    except Exception as e:
        logger.debug(f"Falha ao logar fim da request: {e}")
    return response

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
    { 'qrs': [...], 'bcs': [...], 'codes': set([...]), 'raw': 'QRCodes|Barcodes' }
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

def extract_codes_from_video(video_path: str, frame_skip: int = 10) -> Set[str]:
    codes_found: Set[str] = set()
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"Não foi possível abrir o vídeo: {video_path}")
            return codes_found
        frame_count = 0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        logger.info(f"Processando vídeo com {total_frames} frames...")
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
                if code not in codes_found:
                    logger.info(f"Código encontrado no frame {frame_count}: {code}")
                codes_found.add(code)
        cap.release()
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
# (mantivemos as funções para uso quando necessário)
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
# ======================== ROTAS ==========================
# =========================================================

@app.route('/verificar_caixa', methods=['POST'])
def verificar_caixa_endpoint():
    codigo = request.form.get('codigo')
    if not codigo:
        return render_template('image.html',
                               mensagem_verificacao="Por favor, insira um código para verificar.")
    resultado = verificar_caixa(codigo)
    if resultado == "ok":
        mensagem = f"✅ Caixa com código {codigo} foi encontrada!"
    else:
        mensagem = f"❌ Caixa com código {codigo} não foi encontrada."
    return render_template('image.html',
                           mensagem_verificacao=mensagem,
                           codigo_verificado=codigo)

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

@app.route('/camera')
def camera_page():
    return render_template('camera.html')

@app.route('/image', methods=['GET'])
def imagem_page():
    return render_template('image.html')

@app.route('/ronda/comecar', methods=['POST'])
def ronda_comecar():
    """
    Inicia uma ronda: limpa o arquivo codigos_encontrados.csv (trunca).
    """
    upload_dir = app.config.get('UPLOAD_FOLDER', '.')
    codigos_csv = os.path.join(upload_dir, 'codigos_encontrados.csv')
    try:
        clear_codigos_encontrados(codigos_csv)
        mensagem = "Ronda iniciada: codigos_encontrados.csv limpo."
        logger.info(mensagem)
        return render_template('image.html', mensagens=[mensagem])
    except Exception as e:
        logger.exception("Erro ao iniciar ronda")
        return render_template('image.html', error=f"Erro ao iniciar ronda: {e}")

@app.route('/ronda/encerrar', methods=['POST'])
def ronda_encerrar():
    """
    Encerra a ronda: compara codigos_encontrados.csv com caixas_registradas.csv e exibe relatório.
    O relatório agora também aponta caixas completas/parciais/não encontradas.
    """
    upload_dir = app.config.get('UPLOAD_FOLDER', '.')
    codigos_csv = os.path.join(upload_dir, 'codigos_encontrados.csv')
    caixas_csv = os.path.join(upload_dir, 'caixas_registradas.csv')

    found_codes = read_codigos_encontrados(codigos_csv)
    registered_codes = read_all_registered_codes(caixas_csv)
    registered_boxes = read_registered_boxes(caixas_csv)

    only_found = sorted(list(found_codes - registered_codes))
    only_registered = sorted(list(registered_codes - found_codes))
    common = sorted(list(found_codes & registered_codes))

    # Analisar caixas: se o conjunto de códigos da caixa for subset de found_codes -> box_found
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
    if only_found:
        logger.info(f"Códigos NÃO registrados encontrados na ronda: {only_found}")
    else:
        logger.info("Nenhum código não-registrado encontrado na ronda.")

    return render_template('image.html', relatorio=relatorio, mensagens=[f"Ronda encerrada. Ver relatório."])

@app.route('/image/upload', methods=['POST'])
def upload_image():
    """
    Ao receber imagem:
     - detecta QR/barcodes
     - compara cada código com caixas_registradas.csv (aponta ok/erro)
     - salva os códigos detectados em codigos_encontrados.csv (apendando apenas novos)
     - se a imagem tem >=2 códigos, registra a combinação como uma 'caixa' em caixas_registradas.csv
    """
    if 'image' not in request.files:
        return render_template('image.html', error='Nenhum arquivo enviado.')
    file = request.files['image']
    if file.filename == '':
        return render_template('image.html', error='Nome de arquivo vazio.')
    try:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
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

        # 1) Para cada código, verificar presença em caixas_registradas (não vamos cadastrar individualmente)
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
            # Remover imagem temporária
            try:
                os.remove(filepath)
            except Exception:
                pass
            return render_template('image.html',
                                   error='Nenhum código encontrado na imagem.',
                                   status_codigos=status_codigos)

        # 2) Gravar os códigos detectados em codigos_encontrados.csv (apenas códigos novos no arquivo)
        codigos_csv = os.path.join(upload_dir, 'codigos_encontrados.csv')
        num_new_codes, new_codes_list = save_unique_codes(codigos_csv, qr_codes, barcodes)
        mensagens = []
        if num_new_codes > 0:
            mensagens.append(f"{num_new_codes} código(s) novos adicionados em codigos_encontrados.csv.")
        else:
            mensagens.append("Nenhum código novo para adicionar ao codigos_encontrados.csv.")

        # 3) Se a imagem tem 2 ou mais códigos, registrar como caixa (apenas nessa circunstância)
        caixas_csv = os.path.join(upload_dir, 'caixas_registradas.csv')
        contagem_txt = os.path.join(upload_dir, 'contagem_caixas.txt')

        foi_registrada_caixa = False
        total_caixas = 0
        if len(todos_codigos) >= 2:
            # separar qr vs barcode para persistência consistente
            qr_novos = [c for c in qr_codes]
            barcode_novos = [c for c in barcodes]
            foi_registrada_caixa, total_caixas = register_caixa(caixas_csv, contagem_txt, qr_novos, barcode_novos)
            if foi_registrada_caixa:
                mensagens.append("Imagem continha >=2 códigos — caixa registrada com sucesso.")
            else:
                mensagens.append("Imagem continha >=2 códigos — caixa já estava registrada anteriormente.")

        # Limpeza do arquivo temporário da imagem
        try:
            os.remove(filepath)
            logger.debug(f"Imagem temporária removida: {filepath}")
        except Exception:
            pass

        mensagens.append("Observação: na verificação individual, códigos NÃO registrados são apontados como erro.")
        return render_template(
            'image.html',
            qr_codes=qr_codes,
            barcodes=barcodes,
            mensagens=mensagens,
            status_codigos=status_codigos,
            total_caixas=total_caixas
        )

    except Exception as e:
        logger.exception("Erro ao processar upload de imagem")
        return render_template('image.html', error=f'Erro ao processar a imagem: {str(e)}')

@app.route('/api/processar_video', methods=['POST'])
def processar_video():
    try:
        if 'video' not in request.files:
            return jsonify({"erro": "Nenhum arquivo de vídeo foi enviado"}), 400
        video_file = request.files['video']
        if video_file.filename == '':
            return jsonify({"erro": "Nenhum arquivo selecionado"}), 400
        filename = secure_filename(video_file.filename)
        temp_dir = "/app/temp_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, f"temp_{int(time.time())}_{filename}")
        video_file.save(temp_path)
        logger.info(f"Vídeo salvo temporariamente em: {temp_path}")
        try:
            codes_found = extract_codes_from_video(temp_path, frame_skip=10)
            if not codes_found:
                return jsonify({
                    "message": "Nenhum código de barras ou QR code foi encontrado no vídeo",
                    "codes_found": 0,
                    "results": []
                })
            validation_results = validate_codes_in_database(codes_found)
            success_count = len([r for r in validation_results if "✅" in r["status"]])
            error_count = len([r for r in validation_results if "❌" in r["status"]])
            return jsonify({
                "message": f"Processamento concluído! Encontrados {len(codes_found)} códigos únicos.",
                "codes_found": len(codes_found),
                "success_count": success_count,
                "error_count": error_count,
                "results": validation_results
            })
        finally:
            try:
                os.remove(temp_path)
                logger.info(f"Arquivo temporário removido: {temp_path}")
            except Exception as e:
                logger.warning(f"Não foi possível remover arquivo temporário: {e}")
    except Exception as e:
        logger.exception(f"Erro ao processar vídeo: {e}")
        return jsonify({"erro": f"Erro interno: {str(e)}"}), 500

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
