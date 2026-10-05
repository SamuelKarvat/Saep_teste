import os
import sqlite3
from functools import wraps

from flask import (Flask, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "estoque.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")


# ---------- Banco ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------- Autenticação (RF01, RF02, RF13) ----------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "usuario_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if "usuario_id" in session:
        return redirect(url_for("index"))
    if request.method == "POST":
        login_ = request.form.get("login", "").strip()
        senha = request.form.get("senha", "")
        user = get_db().execute(
            "SELECT * FROM usuarios WHERE login = ?", (login_,)).fetchone()
        if user and check_password_hash(user["senha_hash"], senha):
            session.clear()
            session["usuario_id"] = user["id"]
            session["usuario_nome"] = user["nome"]
            destino = request.args.get("next", "")
            # evita open redirect: só caminhos internos
            if not destino.startswith("/") or destino.startswith("//"):
                destino = url_for("index")
            return redirect(destino)
        flash("Login ou senha inválidos.", "erro")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Sessão encerrada.", "ok")
    return redirect(url_for("login"))


# ---------- Tela principal (RF10) ----------
@app.route("/")
@login_required
def index():
    db = get_db()
    resumo = db.execute("""
        SELECT COUNT(*) AS total_produtos,
               COALESCE(SUM(quantidade), 0) AS total_itens,
               COALESCE(SUM(quantidade * preco), 0) AS valor_estoque,
               COALESCE(SUM(quantidade < estoque_minimo), 0) AS em_alerta
        FROM produtos""").fetchone()
    alertas = db.execute("""
        SELECT p.id, p.nome, p.quantidade, p.estoque_minimo, c.nome AS categoria
        FROM produtos p JOIN categorias c ON c.id = p.categoria_id
        WHERE p.quantidade < p.estoque_minimo
        ORDER BY (p.estoque_minimo - p.quantidade) DESC""").fetchall()
    recentes = db.execute("""
        SELECT m.tipo, m.quantidade, m.data_hora, p.nome AS produto, u.nome AS usuario
        FROM movimentacoes m
        JOIN produtos p ON p.id = m.produto_id
        JOIN usuarios u ON u.id = m.usuario_id
        ORDER BY m.data_hora DESC, m.id DESC LIMIT 5""").fetchall()
    return render_template("index.html", resumo=resumo, alertas=alertas,
                           recentes=recentes)


# ---------- CRUD de produtos (RF03–RF07) ----------
def categorias():
    return get_db().execute("SELECT * FROM categorias ORDER BY nome").fetchall()


def validar_produto(form):
    """Validação no servidor (RNF02). Retorna (dados, erros)."""
    erros = []
    nome = form.get("nome", "").strip()
    espec = form.get("especificacoes", "").strip()
    if not nome:
        erros.append("O nome é obrigatório.")
    try:
        categoria_id = int(form.get("categoria_id", ""))
        if not get_db().execute("SELECT 1 FROM categorias WHERE id=?",
                                (categoria_id,)).fetchone():
            raise ValueError
    except ValueError:
        categoria_id = None
        erros.append("Selecione uma categoria válida.")
    try:
        preco = float(form.get("preco", "").replace(",", "."))
        if preco < 0:
            raise ValueError
    except ValueError:
        preco = None
        erros.append("Preço deve ser um número maior ou igual a zero.")
    try:
        estoque_minimo = int(form.get("estoque_minimo", ""))
        if estoque_minimo < 0:
            raise ValueError
    except ValueError:
        estoque_minimo = None
        erros.append("Estoque mínimo deve ser um inteiro maior ou igual a zero.")
    dados = dict(nome=nome, categoria_id=categoria_id, especificacoes=espec,
                 preco=preco, estoque_minimo=estoque_minimo)
    return dados, erros


@app.route("/produtos")
@login_required
def produtos_lista():
    q = request.args.get("q", "").strip()
    sql = """
        SELECT p.*, c.nome AS categoria
        FROM produtos p JOIN categorias c ON c.id = p.categoria_id"""
    params = []
    if q:
        sql += " WHERE p.nome LIKE ? OR c.nome LIKE ? OR p.especificacoes LIKE ?"
        params = [f"%{q}%"] * 3
    sql += " ORDER BY p.nome COLLATE NOCASE"
    itens = get_db().execute(sql, params).fetchall()
    return render_template("produtos_lista.html", produtos=itens, q=q)


@app.route("/produtos/novo", methods=["GET", "POST"])
@login_required
def produto_novo():
    if request.method == "POST":
        dados, erros = validar_produto(request.form)
        if not erros:
            db = get_db()
            # RN03: quantidade inicial é 0; saldo só muda via movimentações
            db.execute("""INSERT INTO produtos
                (nome, categoria_id, especificacoes, preco, quantidade, estoque_minimo)
                VALUES (:nome, :categoria_id, :especificacoes, :preco, 0, :estoque_minimo)""",
                       dados)
            db.commit()
            flash("Produto cadastrado.", "ok")
            return redirect(url_for("produtos_lista"))
        for e in erros:
            flash(e, "erro")
        return render_template("produto_form.html", produto=request.form,
                               categorias=categorias(), novo=True)
    return render_template("produto_form.html", produto={}, categorias=categorias(),
                           novo=True)


@app.route("/produtos/<int:pid>/editar", methods=["GET", "POST"])
@login_required
def produto_editar(pid):
    db = get_db()
    produto = db.execute("SELECT * FROM produtos WHERE id=?", (pid,)).fetchone()
    if not produto:
        flash("Produto não encontrado.", "erro")
        return redirect(url_for("produtos_lista"))
    if request.method == "POST":
        dados, erros = validar_produto(request.form)
        if not erros:
            dados["id"] = pid
            db.execute("""UPDATE produtos SET nome=:nome, categoria_id=:categoria_id,
                especificacoes=:especificacoes, preco=:preco,
                estoque_minimo=:estoque_minimo WHERE id=:id""", dados)
            db.commit()
            flash("Produto atualizado.", "ok")
            return redirect(url_for("produtos_lista"))
        for e in erros:
            flash(e, "erro")
        return render_template("produto_form.html",
                               produto={**request.form, "id": pid,
                                        "quantidade": produto["quantidade"]},
                               categorias=categorias(), novo=False)
    return render_template("produto_form.html", produto=produto,
                           categorias=categorias(), novo=False)


@app.route("/produtos/<int:pid>/excluir", methods=["POST"])
@login_required
def produto_excluir(pid):
    db = get_db()
    # RF07: produto com movimentações não pode ser excluído
    tem_mov = db.execute("SELECT 1 FROM movimentacoes WHERE produto_id=? LIMIT 1",
                         (pid,)).fetchone()
    if tem_mov:
        flash("Não é possível excluir: o produto possui movimentações.", "erro")
    else:
        db.execute("DELETE FROM produtos WHERE id=?", (pid,))
        db.commit()
        flash("Produto excluído.", "ok")
    return redirect(url_for("produtos_lista"))


if __name__ == "__main__":
    app.run(debug=True)
