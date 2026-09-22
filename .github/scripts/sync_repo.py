#!/usr/bin/env python3
"""
sync_repo.py - High performance synchronization script for APT Debian repositories.
Replaces the slow O(N) bash loop with native in-memory libapt-pkg lookups.
"""

import os
import sys
import glob
import re
import shutil
import argparse
import subprocess

def load_hold_packages(hold_file):
    """
    Reads hold_packages.txt ignoring comments (#) and blank lines.
    Matches package names or full .deb filenames.
    """
    hold = set()
    if os.path.exists(hold_file):
        with open(hold_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    token = line.split()[0]
                    hold.add(token)
    return hold

def parse_stanzas_file(filepath):
    """
    Parses extra_packages.stanzas into a dict: {pkg_name: {"version": str, "stanza": str}}
    """
    stanzas = {}
    if not os.path.exists(filepath):
        return stanzas
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    for block in content.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        pkg_name = None
        version = None
        for line in block.split("\n"):
            if line.startswith("Package: "):
                pkg_name = line[9:].strip()
            elif line.startswith("Version: "):
                version = line[9:].strip()
        if pkg_name and version:
            stanzas[pkg_name] = {"version": version, "stanza": block}
    return stanzas

def write_stanzas_file(filepath, stanzas_dict):
    """
    Writes updated stanzas back to extra_packages.stanzas.
    """
    blocks = [info["stanza"].strip() for info in stanzas_dict.values()]
    content = "\n\n".join(blocks) + "\n"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

def ensure_release_exists(release_tag):
    """
    Ensures that the GitHub Release exists before uploading files to it.
    """
    res = subprocess.run(["gh", "release", "view", release_tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        print(f"==> Creando release '{release_tag}'...")
        subprocess.run([
            "gh", "release", "create", release_tag,
            "--title", "Paquetes Grandes del Repositorio",
            "--notes", "Paquetes grandes que no se almacenan en Git."
        ], check=True)

def generate_stanza_for_large_deb(deb_path, repo_url, release_tag):
    """
    Uses apt-ftparchive to generate standard Debian stanza for deb_path,
    then updates the Filename field to point to the GitHub Release download URL.
    """
    filename = os.path.basename(deb_path)
    res = subprocess.run(["apt-ftparchive", "packages", deb_path], capture_output=True, text=True, check=True)
    stanza = res.stdout.strip()
    download_url = f"https://github.com/{repo_url}/releases/download/{release_tag}/{filename}"
    stanza = re.sub(r"Filename:\s+.*", f"Filename: {download_url}", stanza)
    return stanza

def download_packages(pkgs, dest_dir):
    """
    Downloads a list of packages into dest_dir using apt-get download.
    Attempts batch download first; falls back to individual downloads on failure.
    """
    if not pkgs:
        return
    print(f"==> Descargando {len(pkgs)} paquetes en lote...")
    res = subprocess.run(["apt-get", "download"] + pkgs, cwd=dest_dir)
    if res.returncode != 0:
        print("==> Aviso: La descarga en lote falló. Intentando descarga individual...")
        for p in pkgs:
            sub = subprocess.run(["apt-get", "download", p], cwd=dest_dir)
            if sub.returncode != 0:
                print(f"  [ERROR] No se pudo descargar el paquete: {p}")

def main():
    parser = argparse.ArgumentParser(description="Sync APT repository with upstream Debian Forky")
    parser.add_argument("--repo-dir", default="main/gnulinex-trixie/amd64", help="Repository directory containing .deb files")
    parser.add_argument("--hold-file", default="hold_packages.txt", help="Path to hold_packages.txt")
    parser.add_argument("--extra-stanzas", default="main/gnulinex-trixie/amd64/extra_packages.stanzas", help="Path to extra packages stanzas file")
    parser.add_argument("--dry-run", action="store_true", help="Check only, do not download or update")
    parser.add_argument("--max-size-mb", type=int, default=95, help="Max file size in MB for git before uploading to releases")
    parser.add_argument("--release-tag", default="paquetes-grandes", help="GitHub release tag for large packages")
    args = parser.parse_args()

    # Verify python3-apt is installed
    try:
        import apt
        import apt_pkg
        apt_pkg.init()
    except ImportError:
        print("ERROR: python3-apt no está instalado. Ejecuta: apt-get install -y python3-apt", file=sys.stderr)
        sys.exit(1)

    repo_url = os.environ.get("GITHUB_REPOSITORY", "CarlosGamer98YT/gnulinex-repo")

    print("==> Inicializando caché de APT...")
    cache = apt.Cache()
    print(f"==> Caché cargada con {len(cache)} paquetes disponibles.")

    # 1. Cargar paquetes en hold
    hold_packages = load_hold_packages(args.hold_file)
    print(f"==> Se cargaron {len(hold_packages)} entradas de paquetes retenidos (hold_packages.txt).")

    # 2. Escanear paquetes regulares locales
    to_update = []
    deb_files = glob.glob(os.path.join(args.repo_dir, "*.deb"))
    print(f"==> Escaneando {len(deb_files)} archivos .deb locales...")

    for deb_path in deb_files:
        deb_name = os.path.basename(deb_path)
        parts = deb_name.split("_")
        if len(parts) < 2:
            continue
        pkg_name = parts[0]

        # Comprobar si está bloqueado por nombre o por nombre exacto de .deb
        if pkg_name in hold_packages or deb_name in hold_packages:
            continue

        local_ver = parts[1].replace("%3a", ":")

        if pkg_name in cache:
            cand = cache[pkg_name].candidate
            if cand and cand.version:
                remote_ver = cand.version
                if apt_pkg.version_compare(remote_ver, local_ver) > 0:
                    to_update.append({
                        "name": pkg_name,
                        "local_ver": local_ver,
                        "remote_ver": remote_ver,
                        "old_file": deb_path
                    })

    # 3. Escanear paquetes grandes (>95MB) registrados en extra_packages.stanzas
    extra_stanzas = parse_stanzas_file(args.extra_stanzas)
    large_to_update = []
    for pkg_name, data in extra_stanzas.items():
        if pkg_name in hold_packages:
            continue
        current_ver = data["version"]
        if pkg_name in cache:
            cand = cache[pkg_name].candidate
            if cand and cand.version:
                remote_ver = cand.version
                if apt_pkg.version_compare(remote_ver, current_ver) > 0:
                    large_to_update.append({
                        "name": pkg_name,
                        "local_ver": current_ver,
                        "remote_ver": remote_ver
                    })

    total_updates = len(to_update) + len(large_to_update)
    print(f"==> Paquetes para actualizar: {len(to_update)} regulares, {len(large_to_update)} grandes.")

    for item in to_update:
        print(f"  * [REGULAR] {item['name']}: {item['local_ver']} -> {item['remote_ver']}")
    for item in large_to_update:
        print(f"  * [GRANDE]  {item['name']}: {item['local_ver']} -> {item['remote_ver']}")

    updated_str = "true" if total_updates > 0 else "false"

    # Exportar variables de entorno de GitHub Actions
    github_env = os.environ.get("GITHUB_ENV")
    if github_env and os.path.exists(github_env):
        with open(github_env, "a", encoding="utf-8") as f:
            f.write(f"UPDATED={updated_str}\n")
            f.write(f"UPDATES_COUNT={total_updates}\n")

    # Resumen del paso en GitHub Actions (Step Summary)
    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary and os.path.exists(github_summary):
        with open(github_summary, "a", encoding="utf-8") as f:
            f.write("### Sincronización de Repositorio APT con Debian Forky\n\n")
            if total_updates == 0:
                f.write("✅ **El repositorio está completamente al día.** No se requieren actualizaciones.\n")
            else:
                f.write(f"🔄 **Se detectaron {total_updates} actualizaciones:**\n\n")
                f.write("| Paquete | Tipo | Versión Actual | Versión Nueva |\n")
                f.write("| :--- | :--- | :--- | :--- |\n")
                for item in to_update:
                    f.write(f"| `{item['name']}` | Regular | `{item['local_ver']}` | `{item['remote_ver']}` |\n")
                for item in large_to_update:
                    f.write(f"| `{item['name']}` | Grande (Releases) | `{item['local_ver']}` | `{item['remote_ver']}` |\n")

    if args.dry_run or total_updates == 0:
        print("==> No hay acciones requeridas (modo dry-run o repositorio al día).")
        return

    # Directorio temporal de descargas
    tmp_dl = os.path.join(args.repo_dir, "tmp_dl")
    os.makedirs(tmp_dl, exist_ok=True)

    try:
        # Descarga de paquetes regulares
        if to_update:
            regular_names = [p["name"] for p in to_update]
            download_packages(regular_names, tmp_dl)

            for item in to_update:
                pkg_name = item["name"]
                matches = glob.glob(os.path.join(tmp_dl, f"{pkg_name}_*.deb"))
                if not matches:
                    print(f"  [AVISO] No se encontró el archivo descargado para: {pkg_name}")
                    continue
                new_file = matches[0]
                size_mb = os.path.getsize(new_file) / (1024 * 1024)

                # Si por alguna razón el paquete creció y supera el límite de 95MB
                if size_mb > args.max_size_mb:
                    print(f"  -> Archivo gigante detectado ({size_mb:.1f}MB): {pkg_name}. Subiendo a GitHub Releases...")
                    ensure_release_exists(args.release_tag)
                    subprocess.run(["gh", "release", "upload", args.release_tag, new_file, "--clobber"], check=True)
                    stanza = generate_stanza_for_large_deb(new_file, repo_url, args.release_tag)
                    extra_stanzas[pkg_name] = {"version": item["remote_ver"], "stanza": stanza}
                    write_stanzas_file(args.extra_stanzas, extra_stanzas)
                    if os.path.exists(item["old_file"]):
                        os.remove(item["old_file"])
                    os.remove(new_file)
                else:
                    # Paquete regular normal
                    if os.path.exists(item["old_file"]):
                        os.remove(item["old_file"])
                    shutil.move(new_file, args.repo_dir)
                    print(f"  -> Actualizado {pkg_name}: {item['local_ver']} -> {item['remote_ver']}")

        # Descarga de paquetes grandes específicos
        if large_to_update:
            large_names = [p["name"] for p in large_to_update]
            download_packages(large_names, tmp_dl)

            for item in large_to_update:
                pkg_name = item["name"]
                matches = glob.glob(os.path.join(tmp_dl, f"{pkg_name}_*.deb"))
                if not matches:
                    print(f"  [AVISO] No se encontró el archivo descargado para paquete grande: {pkg_name}")
                    continue
                new_file = matches[0]
                print(f"  -> Subiendo paquete grande a Releases: {os.path.basename(new_file)}...")
                ensure_release_exists(args.release_tag)
                subprocess.run(["gh", "release", "upload", args.release_tag, new_file, "--clobber"], check=True)
                stanza = generate_stanza_for_large_deb(new_file, repo_url, args.release_tag)
                extra_stanzas[pkg_name] = {"version": item["remote_ver"], "stanza": stanza}
                write_stanzas_file(args.extra_stanzas, extra_stanzas)
                os.remove(new_file)
                print(f"  -> Paquete grande actualizado en Releases y stanzas: {pkg_name} ({item['local_ver']} -> {item['remote_ver']})")

    finally:
        shutil.rmtree(tmp_dl, ignore_errors=True)

    print("==> Sincronización finalizada exitosamente.")

if __name__ == "__main__":
    main()
