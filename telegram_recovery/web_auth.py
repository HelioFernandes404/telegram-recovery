"""Loopback-only guided authentication UI for the local Telegram account."""

# The embedded HTML/CSS/JS is intentionally kept together for a dependency-free
# local tool. Python formatting remains linted below this file-level exception.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from telethon import errors

from . import telegram_client
from .auth import (
    API_KEYS,
    ATTEMPTS,
    auth_request,
    configure_credentials_values,
)
from .run_logging import RunLog
from .security import RecoveryError, exclusive_lock, private_file

WEB_PORT = 8766
COOKIE_NAME = "telegram_recovery_web"
MAX_BODY_BYTES = 16 * 1024


PAGE = r'''<!doctype html>
<html lang="pt-BR">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="light dark" />
    <title>Login local · Telegram Recovery</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #f4f6f8;
        --surface: #ffffff;
        --surface-subtle: #f8fafc;
        --text: #17212b;
        --muted: #61707d;
        --line: #dbe2e8;
        --accent: #168aad;
        --accent-dark: #0e6e8a;
        --danger: #a33a45;
        --success: #2f7d51;
        --shadow: 0 18px 48px rgba(23, 33, 43, .10);
      }
      @media (prefers-color-scheme: dark) {
        :root {
          color-scheme: dark;
          --bg: #10171d;
          --surface: #18232c;
          --surface-subtle: #1f2d37;
          --text: #edf4f7;
          --muted: #aabac5;
          --line: #344651;
          --accent: #56c3dc;
          --accent-dark: #86d9ea;
          --danger: #ff9ea6;
          --success: #8bddab;
          --shadow: 0 18px 48px rgba(0, 0, 0, .28);
        }
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        background: radial-gradient(circle at top right, rgba(22, 138, 173, .12), transparent 35%), var(--bg);
        color: var(--text);
        font: 16px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      .shell { width: min(720px, calc(100% - 32px)); margin: 0 auto; padding: 48px 0 64px; }
      .brand { margin-bottom: 28px; }
      .eyebrow { color: var(--accent); font-size: .78rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
      h1 { margin: 8px 0 10px; font-size: clamp(1.8rem, 5vw, 2.6rem); line-height: 1.1; }
      .subtitle { max-width: 570px; margin: 0; color: var(--muted); }
      .card { background: var(--surface); border: 1px solid var(--line); border-radius: 22px; box-shadow: var(--shadow); overflow: hidden; }
      .card-body { padding: clamp(22px, 5vw, 40px); }
      .steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; padding: 18px clamp(22px, 5vw, 40px); border-bottom: 1px solid var(--line); background: var(--surface-subtle); }
      .step { display: flex; gap: 8px; align-items: center; color: var(--muted); font-size: .82rem; }
      .step::before { content: attr(data-number); display: grid; place-items: center; width: 24px; height: 24px; border: 1px solid var(--line); border-radius: 50%; font-size: .75rem; font-weight: 700; }
      .step.active { color: var(--accent-dark); font-weight: 700; }
      .step.active::before { border-color: var(--accent); background: var(--accent); color: white; }
      .step.done { color: var(--success); }
      .step.done::before { border-color: var(--success); color: var(--success); content: "✓"; }
      @media (max-width: 540px) { .step span { display: none; } .step { justify-content: center; } }
      h2 { margin: 0 0 8px; font-size: 1.35rem; }
      .hint { margin: 0 0 24px; color: var(--muted); }
      .notice { margin: 0 0 20px; padding: 12px 14px; border-radius: 12px; background: color-mix(in srgb, var(--accent) 10%, transparent); color: var(--accent-dark); }
      .notice.error { background: color-mix(in srgb, var(--danger) 12%, transparent); color: var(--danger); }
      .notice.success { background: color-mix(in srgb, var(--success) 12%, transparent); color: var(--success); }
      .field { margin: 18px 0; }
      label { display: block; margin-bottom: 7px; font-weight: 650; }
      input { width: 100%; border: 1px solid var(--line); border-radius: 11px; padding: 12px 13px; background: var(--surface); color: var(--text); font: inherit; outline: none; }
      input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
      input[autocomplete="one-time-code"] { letter-spacing: .28em; font-size: 1.35rem; }
      .label-note { display: block; margin-top: 5px; color: var(--muted); font-size: .88rem; }
      button { border: 0; border-radius: 11px; padding: 12px 17px; background: var(--accent); color: white; cursor: pointer; font: inherit; font-weight: 700; }
      button:hover { background: var(--accent-dark); }
      button:disabled { cursor: wait; opacity: .62; }
      button.secondary { background: transparent; color: var(--accent-dark); border: 1px solid var(--line); }
      button.secondary:hover { background: var(--surface-subtle); }
      .actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 26px; }
      .privacy { margin-top: 24px; padding-top: 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: .86rem; }
      .status { display: flex; align-items: center; gap: 10px; color: var(--muted); }
      .spinner { width: 18px; height: 18px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; }
      @keyframes spin { to { transform: rotate(360deg); } }

      /* The login shares the play-video-all visual language. */
      :root {
        color-scheme: light;
        --color-background: #ffffff;
        --color-text-primary: #37352f;
        --color-text-secondary: #787774;
        --color-text-tertiary: #9b9a97;
        --color-surface-sidebar: #f7f7f5;
        --color-surface-card: #ffffff;
        --color-surface-muted: #f1f1ef;
        --color-surface-hover: #37352f0f;
        --color-surface-selected: #37352f14;
        --color-border-subtle: #e9e9e7;
        --color-border-control: #dededb;
        --color-accent: #2383e2;
        --color-accent-soft: #2383e218;
        --color-danger: #d44c47;
        --color-success: #3b8b62;
        --shadow-card: 0 6px 18px #0f0f0f0d;
        --radius-sm: 5px;
        --radius-md: 8px;
        --radius-lg: 12px;
        --font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI Variable Display", "Segoe UI", Helvetica, Arial, sans-serif;
      }
      :root[data-theme="dark"] {
        color-scheme: dark;
        --color-background: #191919;
        --color-text-primary: #e6e6e4;
        --color-text-secondary: #a5a5a3;
        --color-text-tertiary: #858583;
        --color-surface-sidebar: #202020;
        --color-surface-card: #252525;
        --color-surface-muted: #2c2c2c;
        --color-surface-hover: #ffffff0e;
        --color-surface-selected: #ffffff12;
        --color-border-subtle: #303030;
        --color-border-control: #414141;
        --color-accent-soft: #2383e226;
        --color-danger: #eb7874;
        --color-success: #72b48f;
        --shadow-card: 0 6px 18px #00000030;
      }
      html, body {
        min-width: 320px;
        min-height: 100%;
        margin: 0;
        background: var(--color-background);
        color: var(--color-text-primary);
        font-family: var(--font-sans);
        -webkit-font-smoothing: antialiased;
        text-rendering: optimizeLegibility;
      }
      body { overflow: auto; }
      button, input { font: inherit; }
      button:focus-visible, input:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }
      a { color: var(--color-accent); text-decoration: none; }
      a:hover { text-decoration: underline; }
      .app-shell { display: flex; width: 100%; height: 100dvh; min-height: 100dvh; overflow: auto; background: var(--color-background); }
      .sidebar {
        position: relative;
        display: flex;
        flex: 0 0 278px;
        flex-direction: column;
        min-width: 0;
        min-height: 100dvh;
        background: var(--color-surface-sidebar);
        border-inline-end: 1px solid var(--color-border-subtle);
      }
      .sidebar-header { display: flex; align-items: center; min-height: 64px; padding: 12px 14px 10px; }
      .app-brand { display: flex; align-items: center; gap: 9px; min-width: 0; }
      .brand-mark {
        display: grid;
        width: 29px;
        height: 29px;
        flex: 0 0 auto;
        place-items: center;
        border-radius: 7px;
        background: var(--color-text-primary);
        color: var(--color-background);
        font-family: Georgia, serif;
        font-size: 16px;
        font-weight: 700;
      }
      .brand-copy { display: flex; min-width: 0; flex-direction: column; gap: 1px; }
      .brand-copy strong { overflow: hidden; font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
      .brand-copy small { color: var(--color-text-tertiary); font-size: 11px; }
      .sidebar-scroll { flex: 1; padding: 10px 12px 20px; }
      .sidebar-section-heading { margin: 16px 8px 7px; color: var(--color-text-tertiary); font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }
      .nav-row { display: flex; align-items: center; gap: 9px; width: 100%; padding: 7px 8px; border-radius: var(--radius-sm); color: var(--color-text-secondary); font-size: 13px; }
      .nav-row.active { background: var(--color-surface-selected); color: var(--color-text-primary); font-weight: 600; }
      .nav-row.done { color: var(--color-success); }
      .nav-index { width: 22px; color: var(--color-text-tertiary); font-family: var(--font-mono, monospace); font-size: 10px; }
      .nav-row.active .nav-index { color: var(--color-accent); }
      .sidebar-note { margin: 0 8px; color: var(--color-text-secondary); font-size: 12px; line-height: 1.55; }
      .sidebar-footer { display: flex; align-items: center; gap: 7px; padding: 13px 14px 17px; border-top: 1px solid var(--color-border-subtle); color: var(--color-text-tertiary); font-size: 11px; }
      .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--color-success); }
      .main-column { display: flex; flex: 1; min-width: 0; flex-direction: column; }
      .topbar { display: flex; min-height: 64px; align-items: center; justify-content: space-between; gap: 16px; padding: 12px 28px; border-bottom: 1px solid var(--color-border-subtle); background: var(--color-background); }
      .breadcrumbs { display: flex; align-items: center; gap: 8px; color: var(--color-text-tertiary); font-size: 12px; }
      .breadcrumbs strong { color: var(--color-text-primary); font-weight: 500; }
      .offline-status { display: inline-flex; align-items: center; gap: 7px; color: var(--color-text-tertiary); font-size: 11px; }
      .shell { width: min(840px, calc(100% - 64px)); margin: 0 auto; padding: 54px 0 72px; }
      .brand { margin-bottom: 30px; }
      .eyebrow { color: var(--color-text-tertiary); font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
      h1 { margin: 8px 0 10px; color: var(--color-text-primary); font-size: clamp(1.8rem, 4vw, 2.35rem); font-weight: 650; letter-spacing: -.035em; line-height: 1.1; }
      .subtitle { max-width: 610px; margin: 0; color: var(--color-text-secondary); font-size: 14px; }
      .card { background: var(--color-surface-card); border: 1px solid var(--color-border-subtle); border-radius: var(--radius-lg); box-shadow: var(--shadow-card); overflow: hidden; }
      .card-body { padding: 30px 34px 34px; }
      .steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; padding: 0; border-bottom: 1px solid var(--color-border-subtle); background: var(--color-surface-muted); }
      .step { display: flex; gap: 8px; align-items: center; min-height: 48px; padding: 0 16px; color: var(--color-text-tertiary); font-size: 12px; }
      .step::before { content: attr(data-number); display: grid; width: 20px; height: 20px; place-items: center; border: 1px solid var(--color-border-control); border-radius: 50%; font-size: 10px; font-weight: 600; }
      .step.active { color: var(--color-text-primary); font-weight: 600; }
      .step.active::before { border-color: var(--color-accent); background: var(--color-accent); color: white; }
      .step.done { color: var(--color-success); }
      .step.done::before { border-color: var(--color-success); color: var(--color-success); content: "✓"; }
      h2 { margin: 0 0 8px; color: var(--color-text-primary); font-size: 1.28rem; font-weight: 600; letter-spacing: -.02em; }
      .hint { margin: 0 0 24px; color: var(--color-text-secondary); font-size: 14px; }
      .notice { margin: 0 0 20px; padding: 10px 12px; border-radius: var(--radius-md); background: var(--color-accent-soft); color: var(--color-text-secondary); font-size: 13px; }
      .notice.error { background: color-mix(in srgb, var(--color-danger) 12%, transparent); color: var(--color-danger); }
      .notice.success { background: color-mix(in srgb, var(--color-success) 12%, transparent); color: var(--color-success); }
      .field { margin: 18px 0; }
      label { display: block; margin-bottom: 6px; color: var(--color-text-primary); font-size: 13px; font-weight: 600; }
      input { width: 100%; border: 1px solid var(--color-border-control); border-radius: var(--radius-sm); padding: 9px 10px; background: var(--color-surface-card); color: var(--color-text-primary); font-size: 14px; outline: none; }
      input:focus { border-color: var(--color-accent); box-shadow: 0 0 0 2px var(--color-accent-soft); }
      input[autocomplete="one-time-code"] { letter-spacing: .28em; font-size: 1.25rem; }
      .label-note { display: block; margin-top: 5px; color: var(--color-text-tertiary); font-size: 11px; }
      button { border: 0; border-radius: var(--radius-sm); padding: 9px 14px; background: var(--color-accent); color: white; cursor: pointer; font-size: 13px; font-weight: 600; }
      button:hover { filter: brightness(.94); }
      button:disabled { cursor: wait; opacity: .62; }
      button.secondary { background: transparent; color: var(--color-text-secondary); border: 1px solid var(--color-border-control); }
      button.secondary:hover { background: var(--color-surface-hover); filter: none; }
      .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 24px; }
      .privacy { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--color-border-subtle); color: var(--color-text-tertiary); font-size: 12px; }
      .status { display: flex; align-items: center; gap: 10px; color: var(--color-text-secondary); font-size: 13px; }
      .spinner { width: 16px; height: 16px; border: 2px solid var(--color-border-control); border-top-color: var(--color-accent); border-radius: 50%; animation: spin .8s linear infinite; }
      @media (max-width: 720px) {
        .sidebar { flex-basis: 220px; }
        .topbar { padding-inline: 20px; }
        .shell { width: min(100% - 40px, 840px); padding-top: 38px; }
      }
      @media (max-width: 580px) {
        .sidebar { display: none; }
        .topbar { min-height: 56px; }
        .shell { width: calc(100% - 28px); padding: 30px 0 48px; }
        .card-body { padding: 24px 20px 26px; }
        .step { justify-content: center; padding-inline: 6px; }
        .step span { display: none; }
      }
    </style>
  </head>
  <body>
    <div class="app-shell">
      <aside class="sidebar" aria-label="Navegação do login">
        <div class="sidebar-header">
          <div class="app-brand">
            <span class="brand-mark">T</span>
            <span class="brand-copy"><strong>Telegram Recovery</strong><small>Local workspace</small></span>
          </div>
        </div>
        <div class="sidebar-scroll">
          <div class="sidebar-section-heading">Login guiado</div>
          <div class="nav-row active" data-step="credentials"><span class="nav-index">01</span><span>Configuração</span></div>
          <div class="nav-row" data-step="phone"><span class="nav-index">02</span><span>Telefone</span></div>
          <div class="nav-row" data-step="code"><span class="nav-index">03</span><span>Código Telegram</span></div>
          <div class="nav-row" data-step="password"><span class="nav-index">04</span><span>Senha 2FA</span></div>
          <div class="sidebar-section-heading">Segurança</div>
          <p class="sidebar-note">A conexão fica restrita a esta máquina. Os dados são usados somente durante a autenticação local.</p>
        </div>
        <div class="sidebar-footer"><span class="status-dot"></span><span>Somente local</span></div>
      </aside>
      <section class="main-column">
        <header class="topbar">
          <div class="breadcrumbs"><span>Telegram Recovery</span><span>/</span><strong>Login guiado</strong></div>
          <div class="offline-status"><span class="status-dot"></span>Local</div>
        </header>
        <main class="shell">
          <header class="brand">
            <div class="eyebrow">Autenticação</div>
            <h1>Vamos conectar sua conta</h1>
            <p class="subtitle">Um passo de cada vez. Este painel funciona somente nesta máquina e nunca envia seus dados para outro serviço.</p>
          </header>
          <section class="card" aria-live="polite">
            <nav class="steps" aria-label="Etapas da autenticação">
              <div class="step" data-step="credentials" data-number="1"><span>Configuração</span></div>
              <div class="step" data-step="phone" data-number="2"><span>Telefone</span></div>
              <div class="step" data-step="code" data-number="3"><span>Código</span></div>
              <div class="step" data-step="password" data-number="4"><span>2FA</span></div>
            </nav>
            <div class="card-body" id="app"><div class="status"><span class="spinner"></span>Preparando o login local…</div></div>
          </section>
        </main>
      </section>
    </div>
    <script>
      const order = ["credentials", "phone", "code", "password", "success"];
      let current = null;

      const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[char]));
      const app = () => document.getElementById("app");
      const stepIndex = (phase) => ({credentials: 0, phone: 1, code: 2, password: 3, checking: 3, connecting: 1, success: 4, error: 0, cancelled: 0}[phase] ?? 0);

      function updateSteps(phase) {
        const active = stepIndex(phase);
        document.querySelectorAll(".step").forEach((node, index) => {
          node.classList.toggle("active", index === active && active < 4);
          node.classList.toggle("done", index < active || phase === "success");
        });
        document.querySelectorAll(".nav-row[data-step]").forEach((node) => {
          const index = order.indexOf(node.dataset.step);
          node.classList.toggle("active", index === active && active < 4);
          node.classList.toggle("done", index < active || phase === "success");
        });
      }

      function notice(message, kind = "") {
        return message ? `<div class="notice ${kind}">${escapeHtml(message)}</div>` : "";
      }

      function render(state) {
        current = state;
        updateSteps(state.phase);
        if (state.phase === "success") {
          app().innerHTML = `<h2>Conta conectada</h2>${notice(state.message, "success")}<p class="hint">A sessão foi salva localmente com permissões protegidas. Você já pode fechar esta janela.</p>`;
          return;
        }
        if (state.phase === "connecting" || state.phase === "checking") {
          app().innerHTML = `<div class="status"><span class="spinner"></span><div><strong>${escapeHtml(state.title)}</strong><br><span>${escapeHtml(state.message)}</span></div></div>`;
          return;
        }
        if (state.phase === "error" || state.phase === "cancelled") {
          app().innerHTML = `<h2>${state.phase === "cancelled" ? "Login cancelado" : "Não foi possível concluir"}</h2>${notice(state.message, "error")}<div class="actions"><button class="secondary" data-action="restart">Começar novamente</button></div>`;
          return;
        }
        if (state.phase === "credentials") {
          app().innerHTML = `<h2>Primeiro, confira a configuração</h2><p class="hint">Use o API ID e o API hash obtidos em <a href="https://my.telegram.org" target="_blank" rel="noreferrer">my.telegram.org</a>. Eles ficam somente no arquivo local protegido.</p>
            <form id="credentials-form">
              <div class="field"><label for="api-id">API ID</label><input id="api-id" name="api_id" inputmode="numeric" autocomplete="off" required /><span class="label-note">Um número inteiro fornecido pelo Telegram.</span></div>
              <div class="field"><label for="api-hash">API hash</label><input id="api-hash" name="api_hash" spellcheck="false" autocomplete="off" required /><span class="label-note">O valor não aparece novamente depois de salvo.</span></div>
              <div class="actions"><button type="submit">Continuar</button></div>
            </form><p class="privacy">Nada desta tela é salvo no navegador. O backend local recebe os valores apenas para validar e gravar o arquivo protegido.</p>`;
          return;
        }
        if (state.phase === "phone") {
          app().innerHTML = `<h2>Qual telefone vamos autenticar?</h2>${notice(state.message)}<p class="hint">Informe o telefone internacional da sua conta, com + e código do país.</p>
            <form id="phone-form"><div class="field"><label for="phone">Telefone</label><input id="phone" name="phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="+5511999999999" required /></div><div class="actions"><button type="submit">Enviar código</button></div></form>
            <p class="privacy">O código será solicitado uma única vez. Se o Telegram já tiver enviado um código, não vamos reenviá-lo automaticamente.</p>`;
          return;
        }
        if (state.phase === "code") {
          app().innerHTML = `<h2>Digite o código recebido</h2>${notice(state.message, state.error_code ? "error" : "")}<p class="hint">Confira o chat oficial do Telegram ou o cliente em que sua conta já está conectada.</p>
            <form id="code-form"><div class="field"><label for="code">Código de login</label><input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="10" required /></div><div class="actions"><button type="submit">Verificar código</button><button type="button" class="secondary" data-action="cancel">Cancelar</button></div></form>
            <p class="privacy">O código é usado somente para esta tentativa e nunca é gravado em logs.</p>`;
          return;
        }
        if (state.phase === "password") {
          app().innerHTML = `<h2>Confirme sua senha 2FA</h2>${notice(state.message, state.error_code ? "error" : "")}<p class="hint">Sua conta está protegida por verificação em duas etapas. Digite a senha exatamente como no Telegram.</p>
            <form id="password-form"><div class="field"><label for="password">Senha 2FA</label><input id="password" name="password" type="password" autocomplete="current-password" required /></div><div class="actions"><button type="submit">Concluir login</button><button type="button" class="secondary" data-action="cancel">Cancelar</button></div></form>
            <p class="privacy">A senha permanece apenas na memória durante a autenticação e não é exibida nem registrada.</p>`;
        }
      }

      async function request(path, payload = null) {
        const options = payload === null ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)};
        const response = await fetch(path, options);
        const data = await response.json();
        if (!response.ok) throw data.error || {message: "Não foi possível concluir esta etapa."};
        return data;
      }

      async function settle(state) {
        if (!["checking", "connecting"].includes(state.phase)) return state;
        for (let attempt = 0; attempt < 120; attempt += 1) {
          await new Promise((resolve) => setTimeout(resolve, 250));
          const next = (await request("/api/auth/status")).state;
          if (!["checking", "connecting"].includes(next.phase)) return next;
        }
        throw {message: "A validação demorou demais. Confira o terminal e tente novamente."};
      }

      async function load() {
        try { render((await request("/api/auth/status")).state); }
        catch (error) { app().innerHTML = notice(error.message || "Não foi possível carregar o login local.", "error"); }
      }

      document.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.target;
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        try {
          const data = Object.fromEntries(new FormData(form).entries());
          const path = form.id === "credentials-form" || form.id === "phone-form" ? "/api/auth/start" : form.id === "code-form" ? "/api/auth/code" : "/api/auth/password";
          let state = (await request(path, data)).state;
          if (form.id === "credentials-form") form.reset();
          render(state);
          state = await settle(state);
          render(state);
        } catch (error) {
          button.disabled = false;
          const existing = document.querySelector(".notice");
          if (existing) { existing.textContent = error.message || "Não foi possível concluir esta etapa."; existing.classList.add("error"); }
          else app().insertAdjacentHTML("afterbegin", notice(error.message || "Não foi possível concluir esta etapa.", "error"));
        }
      });

      document.addEventListener("click", async (event) => {
        const action = event.target.dataset.action;
        if (!action) return;
        if (action === "cancel") {
          event.target.disabled = true;
          try { render((await request("/api/auth/cancel", {})).state); } catch (error) { alert(error.message); }
        }
        if (action === "restart") {
          try { render((await request("/api/auth/cancel", {})).state); } catch (error) { alert(error.message); }
        }
      });

      load();
    </script>
  </body>
</html>'''


def _normalize_phone(value: str) -> str:
    return re.sub(r"[ ()-]", "", value.strip()) if isinstance(value, str) else ""


def _valid_phone(value: str) -> bool:
    return bool(re.fullmatch(r"\+[1-9][0-9]{6,14}", value))


def _snapshot(state: dict) -> dict:
    return dict(state)


class AuthFlow:
    """Keep one Telethon client and one asyncio loop across browser steps."""

    def __init__(self, root: Path):
        self.root = root.absolute()
        self._state_lock = threading.RLock()
        self._credentials_configured = self._has_credentials()
        self._state = {
            "phase": "phone" if self._credentials_configured else "credentials",
            "title": "Pronto para começar",
            "message": "",
            "attempts": 0,
            "max_attempts": ATTEMPTS,
            "credentials_configured": self._credentials_configured,
            "error_code": None,
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="telegram-recovery-auth", daemon=True)
        self._flow_task: asyncio.Task | None = None
        self._phase_ready: asyncio.Future | None = None
        self._code_future: asyncio.Future | None = None
        self._password_future: asyncio.Future | None = None
        self._closed = False
        self._thread.start()
        if not self._loop_ready.wait(5):
            raise RecoveryError("Não foi possível iniciar o motor do login local.", code="auth_web_unavailable")

    def _has_credentials(self) -> bool:
        try:
            telegram_client.load_credentials(self.root)
        except RecoveryError as exc:
            if exc.code != "credentials_invalid":
                raise
            return False
        return True

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def _call(self, operation, *, timeout=75):
        if self._closed or self._loop is None:
            raise RecoveryError("O login local já foi encerrado.", code="auth_web_closed")
        future = asyncio.run_coroutine_threadsafe(operation(), self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise RecoveryError("O login local demorou demais e foi cancelado.", code="auth_network") from None

    def status(self) -> dict:
        with self._state_lock:
            return _snapshot(self._state)

    def _publish(self, phase: str, *, title: str, message: str, attempts=0, error_code=None):
        with self._state_lock:
            self._state = {
                "phase": phase,
                "title": title,
                "message": message,
                "attempts": attempts,
                "max_attempts": ATTEMPTS,
                "credentials_configured": self._credentials_configured,
                "error_code": error_code,
            }
            current = _snapshot(self._state)
        if (
            phase not in {"connecting", "checking"}
            and self._phase_ready is not None
            and not self._phase_ready.done()
        ):
            self._phase_ready.set_result(current)

    async def _begin(self, phone: str, values: dict[str, str]):
        if self._flow_task is not None and not self._flow_task.done():
            raise RecoveryError("Já existe uma autenticação em andamento nesta janela.", code="auth_web_busy")
        self._phase_ready = self._loop.create_future()
        self._flow_task = self._loop.create_task(self._flow(phone, values))
        self._flow_task.add_done_callback(self._consume_task_exception)
        return await self._phase_ready

    @staticmethod
    def _consume_task_exception(task: asyncio.Task):
        if not task.cancelled():
            task.exception()

    def begin(self, phone: str, *, api_id: str = "", api_hash: str = ""):
        normalized = _normalize_phone(phone or "")
        if not _valid_phone(normalized):
            raise RecoveryError("Informe um telefone internacional válido, como +5511999999999.", code="auth_phone_invalid")
        return self._call(lambda: self._begin(normalized, {API_KEYS[0]: api_id, API_KEYS[1]: api_hash}))

    async def _flow(self, phone: str, values: dict[str, str]):
        configure = bool(values.get(API_KEYS[0]) or values.get(API_KEYS[1]))
        run = RunLog(self.root, command="auth")
        client = None
        session_dir = self.root / "sessions"
        session_path = session_dir / "telegram-recovery.session"
        try:
            with run:
                run.start_auth(configure=configure)
                with exclusive_lock(session_dir / ".inventory.lock"):
                    if configure or not self._credentials_configured:
                        credentials = configure_credentials_values(self.root, values)
                        self._credentials_configured = True
                        run.emit("auth_step", stage="configuration_saved")
                    else:
                        credentials = telegram_client.load_credentials(self.root)
                    for file in session_dir.glob("*.session*"):
                        private_file(file)
                    fd = os.open(session_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    os.fchmod(fd, 0o600)
                    os.close(fd)
                    client = telegram_client.create_client(session_path, credentials)
                    await auth_request(client.connect, "connect")
                    run.emit("auth_step", stage="connected")
                    self._publish("connecting", title="Conectando ao Telegram", message="Verificando se já existe uma sessão autorizada…")
                    authorized = await auth_request(client.is_user_authorized, "authorization")
                    if authorized:
                        user = await auth_request(client.get_me, "user")
                    else:
                        sent = await auth_request(lambda: client.send_code_request(phone), "auth_send_code")
                        code_hash = getattr(sent, "phone_code_hash", None)
                        if not code_hash:
                            raise RecoveryError(
                                "Telegram retornou um fluxo de login não suportado. Verifique o cliente oficial.",
                                code="auth_flow_unsupported",
                            )
                        run.emit("auth_step", stage="code_requested")
                        user = await self._login_with_code(client, phone, code_hash)
                    if user is None or getattr(user, "bot", False):
                        raise RecoveryError(
                            "É necessária uma sessão de conta de usuário, não de bot.", code="bot_session"
                        )
                    run.emit("auth_step", stage="already_authorized" if authorized else "authorized")
                    self._publish("success", title="Conta conectada", message="Sessão de usuário autenticada e salva localmente.")
        except asyncio.CancelledError:
            self._publish("cancelled", title="Login cancelado", message="A autenticação foi cancelada. Nenhum segredo foi gravado nos logs.")
            raise
        except RecoveryError as exc:
            self._publish("error", title="Não foi possível concluir", message=str(exc), error_code=exc.code)
            raise
        except Exception:
            error = RecoveryError(
                "Falha na autenticação. Confira a configuração, a conexão e tente novamente.",
                code="unexpected",
            )
            self._publish("error", title="Não foi possível concluir", message=str(error), error_code=error.code)
            raise error from None
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                finally:
                    for file in session_dir.glob("*.session*"):
                        private_file(file)
            self._code_future = None
            self._password_future = None

    async def _login_with_code(self, client, phone: str, code_hash: str):
        self._code_future = self._loop.create_future()
        self._publish("code", title="Código enviado", message="Confira o Telegram e digite o código recebido.")
        for attempt in range(ATTEMPTS):
            code = await self._code_future
            self._publish("checking", title="Validando código", message="Aguarde um instante…", attempts=attempt + 1)
            try:
                return await auth_request(
                    lambda code=code: client.sign_in(phone=phone, code=code, phone_code_hash=code_hash),
                    "auth_sign_in",
                )
            except errors.SessionPasswordNeededError:
                return await self._login_with_password(client)
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                attempt_number = attempt + 1
                if attempt_number >= ATTEMPTS:
                    raise RecoveryError(
                        "Limite de tentativas de código atingido.", code="auth_attempts_exhausted"
                    ) from None
                self._code_future = self._loop.create_future()
                self._publish(
                    "code",
                    title="Código recusado",
                    message="O código não foi aceito. Confira os dígitos e tente novamente.",
                    attempts=attempt_number,
                    error_code="auth_code_invalid",
                )
        raise RecoveryError("Limite de tentativas de código atingido.", code="auth_attempts_exhausted")

    async def _login_with_password(self, client):
        for attempt in range(ATTEMPTS):
            self._password_future = self._loop.create_future()
            self._publish("password", title="Senha 2FA necessária", message="Sua conta usa verificação em duas etapas.", attempts=attempt)
            password = await self._password_future
            self._publish("checking", title="Validando senha 2FA", message="Aguarde um instante…", attempts=attempt + 1)
            try:
                return await auth_request(lambda password=password: client.sign_in(password=password), "auth_password")
            except errors.PasswordHashInvalidError:
                attempt_number = attempt + 1
                if attempt_number >= ATTEMPTS:
                    raise RecoveryError(
                        "Limite de tentativas de senha 2FA atingido.", code="auth_attempts_exhausted"
                    ) from None
                self._publish(
                    "password",
                    title="Senha 2FA recusada",
                    message="A senha não foi aceita. Tente novamente.",
                    attempts=attempt_number,
                    error_code="auth_password_invalid",
                )
        raise RecoveryError("Limite de tentativas de senha 2FA atingido.", code="auth_attempts_exhausted")

    async def _submit_code(self, code: str):
        code = code.strip() if isinstance(code, str) else ""
        if not re.fullmatch(r"[0-9]{4,10}", code):
            raise RecoveryError("Informe somente os dígitos do código recebido.", code="auth_code_invalid")
        if self._state["phase"] != "code" or self._code_future is None or self._code_future.done():
            raise RecoveryError("Esta etapa do login não está mais disponível.", code="auth_web_state")
        self._code_future.set_result(code)
        await asyncio.sleep(0)
        return self.status()

    def submit_code(self, code: str):
        return self._call(lambda: self._submit_code(code))

    async def _submit_password(self, password: str):
        if not isinstance(password, str) or not password:
            raise RecoveryError("Informe a senha 2FA para continuar.", code="auth_password_invalid")
        if self._state["phase"] != "password" or self._password_future is None or self._password_future.done():
            raise RecoveryError("Esta etapa do login não está mais disponível.", code="auth_web_state")
        self._password_future.set_result(password)
        await asyncio.sleep(0)
        return self.status()

    def submit_password(self, password: str):
        return self._call(lambda: self._submit_password(password))

    async def _cancel(self):
        if self._flow_task is not None and not self._flow_task.done():
            self._flow_task.cancel()
            await asyncio.gather(self._flow_task, return_exceptions=True)
        self._publish("cancelled", title="Login cancelado", message="A autenticação foi cancelada. Você pode começar novamente.")
        return self.status()

    def cancel(self):
        return self._call(self._cancel)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._loop is not None and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._cancel(), self._loop)
            try:
                future.result(5)
            except Exception:
                future.cancel()
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class AuthWebServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, root: Path):
        super().__init__(address, AuthWebHandler)
        self.flow = AuthFlow(root)
        self.cookie_value = secrets.token_urlsafe(32)


class AuthWebHandler(BaseHTTPRequestHandler):
    server: AuthWebServer

    def log_message(self, format, *args):
        # Request paths can contain secrets if a browser is misconfigured; do not log them.
        return

    def _headers(self, content_type: str):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'")

    def _send_json(self, payload: dict, status=HTTPStatus.OK):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_page(self):
        raw = PAGE.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._headers("text/html; charset=utf-8")
        self.send_header("Set-Cookie", f"{COOKIE_NAME}={self.server.cookie_value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        return secrets.compare_digest(cookie.get(COOKIE_NAME, {}).value if cookie.get(COOKIE_NAME) else "", self.server.cookie_value)

    def _require_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise RecoveryError("Requisição inválida.", code="auth_web_request") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise RecoveryError("Requisição inválida.", code="auth_web_request")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RecoveryError("Requisição inválida.", code="auth_web_request") from None
        if not isinstance(data, dict):
            raise RecoveryError("Requisição inválida.", code="auth_web_request")
        return data

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self._send_page()
            return
        if path == "/api/auth/status":
            if not self._authorized():
                self._send_json({"error": {"code": "auth_web_session", "message": "Abra o painel local novamente."}}, HTTPStatus.FORBIDDEN)
                return
            self._send_json({"state": self.server.flow.status()})
            return
        self._send_json({"error": {"code": "not_found", "message": "Rota não encontrada."}}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self._authorized():
            self._send_json({"error": {"code": "auth_web_session", "message": "Sessão do painel local inválida."}}, HTTPStatus.FORBIDDEN)
            return
        try:
            data = self._require_json()
            path = urlsplit(self.path).path
            if path == "/api/auth/start":
                state = self.server.flow.begin(
                    data.get("phone", ""),
                    api_id=data.get("api_id", ""),
                    api_hash=data.get("api_hash", ""),
                )
            elif path == "/api/auth/code":
                state = self.server.flow.submit_code(data.get("code", ""))
            elif path == "/api/auth/password":
                state = self.server.flow.submit_password(data.get("password", ""))
            elif path == "/api/auth/cancel":
                state = self.server.flow.cancel()
            else:
                self._send_json({"error": {"code": "not_found", "message": "Rota não encontrada."}}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"state": state})
        except RecoveryError as exc:
            self._send_json({"error": {"code": exc.code, "message": str(exc)}}, HTTPStatus.BAD_REQUEST)
        except Exception:
            self._send_json(
                {"error": {"code": "auth_web_request", "message": "Não foi possível concluir esta etapa."}},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def serve_auth_web(root: Path, *, port: int = WEB_PORT):
    """Serve the guided UI on loopback until the user stops it with Ctrl+C."""
    if not 1024 <= port <= 65535:
        raise RecoveryError("A porta web deve estar entre 1024 e 65535.", code="auth_web_port")
    server = AuthWebServer(("127.0.0.1", port), root)
    print(f"Frontend de login local: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Login local interrompido.", flush=True)
    finally:
        server.flow.close()
        server.server_close()
