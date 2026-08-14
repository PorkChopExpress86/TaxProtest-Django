#!/usr/bin/env python3
import json
import os
import subprocess
import sys

def zenity(args):
    res = subprocess.run(["zenity"] + args, capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None

def main():
    target_folder = "Environment files"
    project_name = "TaxProtest-Django"
    item_name = f"{project_name} - .env (Production/Dev)"

    if not os.path.isfile(".env"):
        zenity(["--error", "--text=.env file not found in repository root."])
        sys.exit(1)

    # 1. Prompt password
    pw = zenity(["--password", "--title=Bitwarden Master Password", "--text=Enter Bitwarden Master Password to unlock:"])
    if not pw:
        sys.exit(0)

    # 2. Unlock and obtain session key
    unlock_res = subprocess.run(["bw", "unlock", pw, "--raw"], capture_output=True, text=True)
    session = unlock_res.stdout.strip()

    if unlock_res.returncode != 0 or not session:
        err = unlock_res.stderr.strip() or unlock_res.stdout.strip()
        zenity(["--error", f"--text=Unlock Failed:\n{err}"])
        sys.exit(1)

    env = os.environ.copy()
    env["BW_SESSION"] = session

    # 3. Sync Vault with session
    subprocess.run(["bw", "sync", "--session", session], env=env, capture_output=True)

    # 4. Find or Create "Environment files" folder
    folders_raw = subprocess.run(["bw", "list", "folders", "--session", session], env=env, capture_output=True, text=True).stdout
    folder_id = None
    try:
        for f in json.loads(folders_raw):
            if f.get("name", "").strip().lower() == target_folder.lower():
                folder_id = f.get("id")
                break
    except Exception:
        pass

    if not folder_id:
        f_payload = json.dumps({"name": target_folder})
        enc_f = subprocess.run(["bw", "encode"], input=f_payload, capture_output=True, text=True).stdout.strip()
        f_res = subprocess.run(["bw", "create", "folder", "--session", session], input=enc_f, env=env, capture_output=True, text=True)
        if f_res.returncode == 0 and f_res.stdout:
            try:
                folder_id = json.loads(f_res.stdout).get("id")
            except Exception:
                pass

    # 5. Read .env and create or update item in Bitwarden
    with open(".env", "r") as f:
        env_content = f.read()

    items_raw = subprocess.run(["bw", "list", "items", "--search", item_name, "--session", session], env=env, capture_output=True, text=True).stdout
    existing_id = None
    try:
        for it in json.loads(items_raw):
            if it.get("name") == item_name:
                existing_id = it.get("id")
                break
    except Exception:
        pass

    # Base secure note template directly without calling 'bw get template'
    item_obj = {
        "type": 2,
        "name": item_name,
        "notes": env_content,
        "secureNote": {"type": 0}
    }
    if folder_id:
        item_obj["folderId"] = folder_id

    payload = json.dumps(item_obj)
    encoded_item = subprocess.run(["bw", "encode"], input=payload, capture_output=True, text=True).stdout.strip()

    if existing_id:
        save_res = subprocess.run(["bw", "edit", "item", existing_id, "--session", session], input=encoded_item, env=env, capture_output=True, text=True)
    else:
        save_res = subprocess.run(["bw", "create", "item", "--session", session], input=encoded_item, env=env, capture_output=True, text=True)

    if save_res.returncode == 0:
        msg = f"✓ Successfully backed up .env to Bitwarden!\n\nItem: {item_name}\nFolder: {target_folder}"
        zenity(["--info", f"--text={msg}"])
        print("SUCCESS")
    else:
        err = save_res.stderr.strip() or save_res.stdout.strip()
        zenity(["--error", f"--text=Failed to save item:\n{err}"])
        print(f"FAILED: {err}")
        sys.exit(1)

if __name__ == "__main__":
    main()
