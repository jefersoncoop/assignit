# app.py

import os
import uuid
import json
import hashlib
from datetime import datetime, UTC
import shutil
import cv2
import io
from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy 
from flask_cors import CORS # <-- NOVA LINHA 1
from werkzeug.utils import secure_filename

import fitz
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from PyPDF2 import PdfWriter, PdfReader

# --- Configuração do App e Pastas ---
app = Flask(__name__)
CORS(app) # <-- NOVA LINHA 2
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'assinaturas.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['PENDING_FOLDER'] = os.path.join(BASE_DIR, 'pending')
app.config['SIGNED_FOLDER'] = os.path.join(BASE_DIR, 'signed')
app.config['COMPLETED_FOLDER'] = os.path.join(BASE_DIR, 'completed')
app.config['TEMPLATES_PDF_FOLDER'] = os.path.join(BASE_DIR, 'templates_pdf')

for folder_key in ['PENDING_FOLDER', 'SIGNED_FOLDER', 'COMPLETED_FOLDER', 'TEMPLATES_PDF_FOLDER']:
    os.makedirs(app.config[folder_key], exist_ok=True)

db = SQLAlchemy(app)

# --- Modelo do Banco de Dados ---
class Documento(db.Model):
    request_id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    status = db.Column(db.String(20), default='pending')
    original_filename = db.Column(db.String(255))
    original_hash = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC))
    signer_name = db.Column(db.String(255))
    signer_cpf = db.Column(db.String(20))
    signer_dob = db.Column(db.String(20), nullable=True)
    doc_data = db.Column(db.JSON, nullable=True)
    audit_ip = db.Column(db.String(45), nullable=True)
    audit_user_agent = db.Column(db.String(255), nullable=True)
    audit_timestamp = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "status": self.status,
            "nome_arquivo": self.original_filename,
            "nome_signatario": self.signer_name,
            "cpf_signatario": self.signer_cpf,
            "data_criacao": self.created_at.isoformat() if self.created_at else None,
            "data_assinatura": self.audit_timestamp.isoformat() if self.audit_timestamp else None
        }

# --- Funções Auxiliares ---
def calculate_hash(filepath):
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def mask_cpf(cpf):
    if not cpf: return "***.***.***-**"
    cpf_numerico = ''.join(filter(str.isdigit, cpf))
    if len(cpf_numerico) != 11:
        return f"***.{cpf_numerico[3:6]}.{cpf_numerico[6:9]}-**"
    return f"***.{cpf_numerico[3:6]}.{cpf_numerico[6:9]}-**"

# --- Rotas da API ---
@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "API de assinaturas digitais está online."}), 200

@app.route('/api/criar-solicitacao', methods=['POST'])
def create_signature_api():
    if 'documento' not in request.files:
        return jsonify({"sucesso": False, "erro": "Nenhum arquivo PDF foi enviado no campo 'documento'."}), 400
    file = request.files['documento']
    if file.filename == '' or not file.filename.lower().endswith('.pdf'):
        return jsonify({"sucesso": False, "erro": "O arquivo enviado é inválido ou não é um PDF."}), 400
    dados_signatario = request.form
    campos_obrigatorios = ['nome', 'cpf', 'data_nascimento']
    if not all(campo in dados_signatario and dados_signatario[campo] for campo in campos_obrigatorios):
        return jsonify({"sucesso": False, "erro": f"O campo '{campo}' é obrigatório."}), 400
            
    request_id = str(uuid.uuid4())
    pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
    os.makedirs(pending_path)
    
    filename = secure_filename(file.filename)
    temp_filepath = os.path.join(pending_path, filename)
    file.save(temp_filepath)
    original_hash = calculate_hash(temp_filepath)
    
    try:
        new_doc = Documento(
            request_id=request_id,
            signer_name=dados_signatario['nome'],
            signer_cpf=dados_signatario['cpf'],
            signer_dob=dados_signatario['data_nascimento'],
            original_filename=filename,
            original_hash=original_hash
        )
        db.session.add(new_doc)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "erro": f"Erro de banco de dados: {str(e)}"}), 500
        
    signing_link = url_for('sign_document', request_id=request_id, _external=True)
    return jsonify({ "sucesso": True, "request_id": request_id, "signing_link": signing_link }), 201

@app.route('/api/criar-por-modelo', methods=['POST'])
def create_from_template_api():
    dados = request.json
    if not dados:
        return jsonify({"sucesso": False, "erro": "Request deve ser do tipo JSON."}), 400
        
    campos_obrigatorios = ['nome', 'cpf', 'conta', 'banco', 'agencia', 'tipoconta', 'telefone', 'email']
    if not all(campo in dados and dados[campo] for campo in campos_obrigatorios):
        return jsonify({"sucesso": False, "erro": f"Campos JSON obrigatórios: {campos_obrigatorios}"}), 400

    request_id = str(uuid.uuid4())
    pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
    os.makedirs(pending_path)

    try:
        template_path = os.path.join(app.config['TEMPLATES_PDF_FOLDER'], 'PEDIDO DE DESLIGAMENTO V5.pdf')
        if not os.path.exists(template_path):
             return jsonify({"sucesso": False, "erro": "PDF modelo 'PEDIDO DE DESLIGAMENTO V5.pdf' não encontrado."}), 500
             
        final_pdf_name = f"documento_preenchido_{request_id}.pdf"
        output_pdf_path = os.path.join(pending_path, final_pdf_name)
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=letter)
        c.drawString(80, 380, f"COOPERADO: {dados['nome']}")
        c.drawString(80, 360, f"DADOS BANCARIOS")
        c.drawString(80, 340, f"BANCO: {dados['banco']}")
        c.drawString(80, 320, f"AGÊNCIA: {dados['agencia']}")
        c.drawString(80, 300, f"CONTA: {dados['conta']}")
        c.drawString(80, 280, f"TIPO DE CONTA: {dados['tipoconta']}")
        c.drawString(80, 260, f"CPF: {dados['cpf']}")
        c.drawString(80, 240, f"TELEFONE : {dados['telefone']}")
        c.drawString(80, 220, f"E-MAIL: {dados['email']}")
        c.save()
        packet.seek(0)
        
        data_pdf = PdfReader(packet)
        template_pdf = PdfReader(open(template_path, "rb"))
        output_writer = PdfWriter()

        page = template_pdf.pages[0]
        page.merge_page(data_pdf.pages[0])
        output_writer.add_page(page)
        
        for page_num in range(1, len(template_pdf.pages)):
            output_writer.add_page(template_pdf.pages[page_num])

        with open(output_pdf_path, "wb") as f:
            output_writer.write(f)
    except Exception as e:
        print(f"Erro ao gerar PDF: {e}")
        return jsonify({"sucesso": False, "erro": "Falha interna ao gerar o PDF."}), 500

    original_hash = calculate_hash(output_pdf_path)
    try:
        new_doc = Documento(
            request_id=request_id,
            signer_name=dados['nome'],
            signer_cpf=dados['cpf'],
            doc_data=dados, 
            original_filename=final_pdf_name,
            original_hash=original_hash
        )
        db.session.add(new_doc)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "erro": f"Erro de banco de dados: {str(e)}"}), 500
        
    signing_link = url_for('sign_document', request_id=request_id, _external=True)
    return jsonify({ "sucesso": True, "request_id": request_id, "signing_link": signing_link, "download_link":"https://assign.tec.br/download/signed_"+final_pdf_name }), 201

# --- Rotas do Processo de Assinatura ---
@app.route('/sign/<request_id>', methods=['GET'])
def sign_document(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc:
        return "<h1>Link inválido</h1><p>Esta solicitação de assinatura não foi encontrada.</p>", 404
    if doc.status != 'pending':
        return "<h1>Link expirado</h1><p>Esta solicitação de assinatura já foi concluída ou cancelada.</p>", 403

    pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
    pdf_path = os.path.join(pending_path, doc.original_filename)
    if not os.path.exists(pdf_path):
        return "<h1>Erro Interno</h1><p>O arquivo PDF original não foi encontrado.</p>", 500
        
    doc_fitz = fitz.open(pdf_path)
    image_paths = []
    for page_num in range(len(doc_fitz)):
        page = doc_fitz.load_page(page_num)
        pix = page.get_pixmap()
        image_filename = f"page_{page_num + 1}.png"
        image_filepath = os.path.join(pending_path, image_filename)
        pix.save(image_filepath)
        image_paths.append(url_for('get_pending_file', request_id=request_id, filename=image_filename))
        
    return render_template('sign_document.html', 
                           request_id=request_id, 
                           document_images=image_paths, 
                           signer_name=doc.signer_name, 
                           masked_cpf=mask_cpf(doc.signer_cpf))

@app.route('/pending/<request_id>/<filename>')
def get_pending_file(request_id, filename):
    directory = os.path.join(app.config['PENDING_FOLDER'], request_id)
    return send_from_directory(directory, filename)

@app.route('/submit_signature/<request_id>', methods=['POST'])
def submit_signature(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc or doc.status != 'pending':
        abort(404, "Solicitação inválida ou já concluída.")

    pending_path = os.path.join(app.config['PENDING_FOLDER'], doc.request_id)
    
    # Validação da selfie
    signature_b64 = request.form['signature'].split(',')[1]
    selfie_b64 = request.form['selfie'].split(',')[1]
    import base64
    signature_img_path = os.path.join(pending_path, 'signature.png')
    selfie_img_path = os.path.join(pending_path, 'selfie.png')
    with open(signature_img_path, "wb") as f: f.write(base64.b64decode(signature_b64))
    with open(selfie_img_path, "wb") as f: f.write(base64.b64decode(selfie_b64))
    try:
        face_cascade = cv2.CascadeClassifier('haarcascade_frontalface_default.xml')
        img = cv2.imread(selfie_img_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        if len(faces) == 0:
            return "<h1>Erro de Validação</h1><p>Nenhum rosto foi detectado na selfie. Tente novamente.</p>", 400
    except Exception as e:
        print(f"Erro durante a validação facial: {e}")
        return "<h1>Erro Interno</h1><p>Ocorreu um erro ao processar a validação da selfie.</p>", 500

    # Geração da Página de Auditoria
    audit_pdf_path = os.path.join(pending_path, 'audit_page.pdf')
    c = canvas.Canvas(audit_pdf_path, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, height - 72, "Página de Auditoria da Assinatura Eletrônica")
    text_y = height - 120
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, text_y, "Detalhes do Documento Original")
    c.setFont("Helvetica", 10)
    text_y -= 20; c.drawString(72, text_y, f"Nome do Arquivo: {doc.original_filename}")
    text_y -= 20; c.drawString(72, text_y, f"Hash SHA256: {doc.original_hash}")
    
    text_y -= 40
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, text_y, "Detalhes do Signatário e Documento")
    c.setFont("Helvetica", 10)
    text_y -= 20; c.drawString(72, text_y, f"Nome: {doc.signer_name}")
    text_y -= 20; c.drawString(72, text_y, f"CPF: {doc.signer_cpf}")
    if doc.signer_dob:
        text_y -= 20; c.drawString(72, text_y, f"Data de Nascimento: {doc.signer_dob}")

    if doc.doc_data:
        campos_doc = {
            'banco': 'Banco', 'agencia': 'Agência', 'conta': 'Conta',
            'tipoconta': 'Tipo de Conta', 'telefone': 'Telefone', 'email': 'E-mail'
        }
        for chave, rotulo in campos_doc.items():
            if chave in doc.doc_data:
                text_y -= 20; c.drawString(72, text_y, f"{rotulo}: {doc.doc_data[chave]}")
    
    audit_timestamp = datetime.now(UTC)
    audit_ip = request.remote_addr
    audit_user_agent = request.headers.get('User-Agent')

    text_y -= 20; c.drawString(72, text_y, f"Endereço IP: {audit_ip}")
    text_y -= 20; c.drawString(72, text_y, f"Data/Hora (UTC): {audit_timestamp.isoformat()}")
    text_y -= 20; c.drawString(72, text_y, f"Navegador: {audit_user_agent}")
    
    text_y -= 40
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, text_y, "Evidências Coletadas")
    c.drawString(72, text_y - 20, "Assinatura:"); sig_img = ImageReader(signature_img_path)
    c.drawImage(sig_img, 72, text_y - 140, width=200, height=100, preserveAspectRatio=True, mask='auto')
    c.drawString(350, text_y - 20, "Selfie com Documento:"); selfie_img = ImageReader(selfie_img_path)
    c.drawImage(selfie_img, 350, text_y - 140, width=120, height=90, preserveAspectRatio=True, mask='auto')
    c.save()

    # --- Junção dos PDFs (CORRIGIDO) ---
    original_pdf_path = os.path.join(pending_path, doc.original_filename)
    output_pdf = PdfWriter()
    
    # Abre, lê e adiciona o PDF original
    with open(original_pdf_path, 'rb') as f_orig:
        reader_orig = PdfReader(f_orig)
        for page in reader_orig.pages:
            output_pdf.add_page(page)
    
    # Abre, lê e adiciona o PDF de auditoria
    with open(audit_pdf_path, 'rb') as f_audit:
        reader_audit = PdfReader(f_audit)
        output_pdf.add_page(reader_audit.pages[0])
    
    # Salva o arquivo final
    final_filename = f"signed_{doc.original_filename}"
    final_filepath = os.path.join(app.config['SIGNED_FOLDER'], final_filename)
    with open(final_filepath, 'wb') as f_final:
        output_pdf.write(f_final)
    # --- FIM DA CORREÇÃO ---
    
    # Atualiza o Banco de Dados
    try:
        doc.status = 'signed'
        doc.audit_ip = audit_ip
        doc.audit_user_agent = audit_user_agent
        doc.audit_timestamp = audit_timestamp
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao atualizar DB: {e}")
        return "<h1>Erro Interno</h1><p>Ocorreu um erro ao salvar a assinatura.</p>", 500
    
    # Move a pasta de trabalho para 'completed'
    destination_path = os.path.join(app.config['COMPLETED_FOLDER'], request_id)
    shutil.move(pending_path, destination_path)
    
    return redirect(url_for('success', filename=final_filename))

# --- Outras Rotas ---
@app.route('/success')
def success():
    filename = request.args.get('filename')
    return render_template('success.html', filename=filename)

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(app.config['SIGNED_FOLDER'], filename, as_attachment=True)
    
@app.route('/api/documentos', methods=['GET'])
def listar_documentos():
    try:
        docs = Documento.query.order_by(Documento.created_at.desc()).all()
        return jsonify([doc.to_dict() for doc in docs])
    except Exception as e:
        return jsonify({"sucesso": False, "erro": str(e)}), 500

# --- Comando para criar o banco de dados ---
@app.cli.command("create-db")
def create_db():
    """Cria as tabelas do banco de dados."""
    with app.app_context():
        db.create_all()
    print("Banco de dados criado com sucesso!")

if __name__ == '__main__':
    with app.app_context():
        db.create_all() 
    app.run(debug=True, port=5001)