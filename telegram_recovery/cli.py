"""Local CLI with explicit authentication, metadata inventory and grouped downloads."""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .auth import authenticate_user, require_terminal
from .database import Database, utc_now
from .download_plan import build_plan, plan_summary
from .downloader import download_items
from .inventory import InventoryFilter, inventory_channel, inventory_message
from .module_check import check_modules
from .run_logging import ACTIVE_RUN, RunLog
from .security import RecoveryError, exclusive_lock
from .telegram_client import channel_number, existing_user_session, resolve_channel
from .web_auth import WEB_PORT, serve_auth_web


def positive_int(value: str) -> int:
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Informe um inteiro positivo.")


def channel_argument(value: str) -> int:
    try:
        return channel_number(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def section_number(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,6}", value):
        raise argparse.ArgumentTypeError("Informe o número do curso/módulo, como 116 ou 001.")
    return int(value)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="telegram-recovery",
        description="Inventário e backup local de vídeos do Telegram por curso e módulo.",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Pasta local com .env, sessions/ e data/ (padrão: diretório atual).",
    )
    inv = commands.add_parser(
        "inventory", parents=[common], help="Inventariar metadados de vídeos."
    )
    inv.add_argument("--channel", type=channel_argument, required=True)
    inv.add_argument(
        "--message-id",
        type=positive_int,
        help="Consultar uma mensagem específica sem alterar o checkpoint do histórico.",
    )
    inv.add_argument(
        "--tag", action="append", default=[], help="F2072 ou '#F2072'; repetível (OU)."
    )
    inv.add_argument("--from-tag", help="Limite numérico inferior inclusivo, como F2072.")
    inv.add_argument("--to-tag", help="Limite numérico superior inclusivo, como F2090.")
    inv.add_argument(
        "--limit",
        type=positive_int,
        help="Máximo de mensagens examinadas nesta execução, incluindo as sem vídeo/tag.",
    )
    inv.add_argument("--page-size", type=positive_int, default=100, help="1 a 100; padrão: 100.")
    inv.add_argument(
        "--verbose",
        action="store_true",
        help="Mostrar também os eventos estruturados no terminal; nunca inclui dados sensíveis.",
    )
    inv.add_argument(
        "--rescan",
        action="store_true",
        help="Recomeçar do início para rever metadados antigos; preserva todos os registros.",
    )
    commands.add_parser("status", parents=[common], help="Resumo local do manifesto, sem rede.")
    report = commands.add_parser("report", parents=[common], help="Manifesto local em JSON Lines.")
    report.add_argument("--channel", type=channel_argument)
    auth = commands.add_parser("auth", parents=[common], help="Autenticar no terminal local.")
    auth.add_argument(
        "--configure",
        action="store_true",
        help="Preencher campos ausentes do .env com entrada oculta; preserva valores existentes.",
    )
    auth.add_argument(
        "--web",
        action="store_true",
        help="Abrir o assistente de login local no navegador, limitado a 127.0.0.1.",
    )
    auth.add_argument(
        "--port",
        type=int,
        default=WEB_PORT,
        help=f"Porta do assistente web (padrão: {WEB_PORT}).",
    )
    download = commands.add_parser(
        "download", parents=[common], help="Baixar vídeos por curso/módulo, com retomada."
    )
    download.add_argument("--channel", type=channel_argument, required=True)
    download.add_argument("--concurrency", type=positive_int, default=1)
    download.add_argument("--course", type=section_number, help="Número do curso, como 116.")
    download.add_argument("--module", type=section_number, help="Número do módulo; exige --course.")
    download.add_argument("--message-id", type=positive_int)
    download.add_argument("--tag", action="append", default=[])
    download.add_argument("--from-tag")
    download.add_argument("--to-tag")
    download.add_argument("--limit", type=positive_int, help="Máximo de vídeos selecionados.")
    download.add_argument(
        "--dry-run", action="store_true", help="Prévia local por módulos; sem rede."
    )
    download.add_argument("--verbose", action="store_true")
    commands.add_parser("verify", help="Reservado; verificação indisponível nesta etapa.")
    return result


def select_downloads(database, args, filters):
    return build_plan(
        database,
        args.channel,
        course=args.course,
        module=args.module,
        message_id=args.message_id,
        limit=args.limit,
        filters=filters,
    )


async def run_download(args, filters, run):
    root = args.root.absolute()
    with exclusive_lock(root / "data" / ".inventory.lock"):
        with Database(root / "data" / "manifest.sqlite3") as database:
            items = select_downloads(database, args, filters)
            run.emit(
                "download_started",
                channel_id=args.channel,
                selected=len(items),
                concurrency=args.concurrency,
                course=args.course,
                module=args.module,
            )
            if not items:
                print("Nenhum vídeo selecionado no manifesto; revise filtros/inventário.")
                return
            checked = await check_modules(database, root, items, progress=run.progress)
            for module in checked.modules:
                run.emit("module_checked", **module)
            for item in checked.completed:
                video = item.video
                saved = database.download(video["channel_id"], video["message_id"])
                if (
                    saved["status"] != "downloaded"
                    or saved["error_code"]
                    or not saved["downloaded_at"]
                ):
                    database.update_download(
                        video["channel_id"],
                        video["message_id"],
                        status="downloaded",
                        error_code=None,
                        downloaded_at=saved["downloaded_at"] or utc_now(),
                    )
                run.emit(
                    "download_skipped",
                    channel_id=video["channel_id"],
                    message_id=video["message_id"],
                    bytes_downloaded=video["expected_size"],
                )

            async def transfer(client=None, entity=None):
                return await download_items(
                    client,
                    entity,
                    database,
                    root,
                    checked.pending,
                    concurrency=args.concurrency,
                    progress=run.progress,
                    already_skipped=len(checked.completed),
                    modules_skipped=checked.completed_modules,
                )

            if checked.pending:
                async with existing_user_session(root, progress=run.progress) as client:
                    entity = await resolve_channel(client, args.channel, progress=run.progress)
                    counts = await transfer(client, entity)
            else:
                counts = await transfer()
            print(json.dumps(counts, ensure_ascii=False))
            if counts["failed"]:
                raise RecoveryError(
                    "Alguns downloads falharam; arquivos preservados. Consulte o log.",
                    code="download_failed",
                )


async def run_inventory(args, filters):
    def progress(message):
        run = ACTIVE_RUN.get()
        if run is not None:
            run.progress(message)
        else:
            print(message, file=sys.stderr, flush=True)

    root = args.root.absolute()
    with exclusive_lock(root / "data" / ".inventory.lock"):
        async with existing_user_session(root, progress=progress) as client:
            entity = await resolve_channel(client, args.channel, progress=progress)
            with Database(root / "data" / "manifest.sqlite3") as database:
                if args.message_id is not None:
                    single = await inventory_message(
                        client, entity, args.channel, database, args.message_id, progress=progress
                    )
                    print(
                        f"Resumo individual: mensagem={single.message_id}; "
                        f"resultado={single.outcome}; novos={single.inserted}; "
                        f"atualizados={single.updated}. Checkpoint do histórico preservado."
                    )
                    if single.outcome in ("missing", "non_video"):
                        progress("Mensagem ausente ou sem vídeo; registros anteriores preservados.")
                    return
                summary = await inventory_channel(
                    client,
                    entity,
                    args.channel,
                    database,
                    filters=filters,
                    limit=args.limit,
                    page_size=args.page_size,
                    rescan=args.rescan,
                    progress=progress,
                )
                print(
                    f"Resumo: estado={summary.status}; lidas={summary.scanned}; "
                    f"vídeos={summary.videos_seen}; selecionados={summary.matched}; "
                    f"novos={summary.inserted}; atualizados={summary.updated}; "
                    f"sem_tag={summary.without_tags}; cursor={summary.last_message_id}."
                )


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.command == "verify":
        print(
            f"{args.command}: reservado para a próxima etapa; nenhuma ação executada.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.command == "auth":
            if args.web:
                if args.configure:
                    raise RecoveryError(
                        "Use os campos de configuração do assistente web; --configure é exclusivo "
                        "do terminal.",
                        code="auth_web_options",
                    )
                serve_auth_web(args.root.absolute(), port=args.port)
                return 0
            require_terminal()
            with RunLog(args.root.absolute(), command="auth") as run:
                run.start_auth(configure=args.configure)
                asyncio.run(authenticate_user(args.root.absolute(), configure=args.configure))
        elif args.command == "inventory":
            if args.message_id is not None and (
                args.limit is not None
                or args.rescan
                or args.tag
                or args.from_tag
                or args.to_tag
                or args.page_size != 100
            ):
                arguments.error(
                    "--message-id não pode ser combinado com filtros, --limit, "
                    "--rescan ou --page-size diferente do padrão."
                )
            try:
                filters = InventoryFilter(tuple(args.tag), args.from_tag, args.to_tag)
            except ValueError:
                print(
                    "Erro: filtro inválido. Use F2072 e um intervalo crescente de tags.",
                    file=sys.stderr,
                )
                return 2
            if args.page_size > 100:
                arguments.error("--page-size deve estar entre 1 e 100.")
            with RunLog(args.root.absolute(), verbose=args.verbose) as run:
                run.start(args, filters)
                asyncio.run(run_inventory(args, filters))
        elif args.command == "download":
            if args.module is not None and args.course is None:
                arguments.error("--module exige --course para identificar o módulo.")
            if args.concurrency > 8:
                arguments.error("--concurrency deve estar entre 1 e 8.")
            try:
                filters = InventoryFilter(tuple(args.tag), args.from_tag, args.to_tag)
            except ValueError:
                print("Erro: filtro inválido. Use F2072 e intervalo crescente.", file=sys.stderr)
                return 2
            path = args.root.absolute() / "data" / "manifest.sqlite3"
            if not path.exists():
                raise RecoveryError("Manifesto ausente. Execute inventory antes do download.")
            if args.dry_run:
                with Database(path, readonly=True) as database:
                    items = select_downloads(database, args, filters)
                    checked = asyncio.run(check_modules(database, args.root.absolute(), items))
                    print(
                        json.dumps(
                            {**plan_summary(items), **checked.summary()},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
            else:
                with RunLog(args.root.absolute(), command="download", verbose=args.verbose) as run:
                    asyncio.run(run_download(args, filters, run))
        else:
            path = args.root.absolute() / "data" / "manifest.sqlite3"
            if not path.exists():
                print("Manifesto ainda não criado. Nenhuma conexão com o Telegram foi feita.")
                return 0
            with Database(path, readonly=True) as database:
                if args.command == "status":
                    print(json.dumps(database.summary(), ensure_ascii=False, indent=2))
                else:
                    for record in database.videos(args.channel):
                        print(json.dumps(record, ensure_ascii=False))
        return 0
    except RecoveryError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        message = (
            "Autenticação interrompida; execute auth novamente quando desejar."
            if (args.command == "auth")
            else "Interrompido. Repita download com os mesmos filtros para retomar os parciais."
            if args.command == "download"
            else "Interrompido. Retome com o mesmo inventory, sem --rescan."
        )
        print(message, file=sys.stderr)
        return 130
    except (OSError, sqlite3.Error):
        print("Erro de acesso local/rede. Dados já confirmados foram preservados.", file=sys.stderr)
        return 1
    except Exception:
        # Do not print raw Telethon exceptions, tracebacks or request payloads.
        print(
            "Falha na operação. Confira a configuração, a sessão e o acesso ao Telegram; "
            "dados já confirmados foram preservados. Detalhes sensíveis foram omitidos.",
            file=sys.stderr,
        )
        return 1
