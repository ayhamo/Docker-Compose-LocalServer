import json
import os
import subprocess

# Updated paths for "Phone Photos"
json_path = r"/mnt/hdd/docker/nextcloud-data/ayham/files/Media & Photos/Phone Photos/albums-info.json"
nc_user = "ayham"
relative_base_path = "Media & Photos/Phone Photos"

if not os.path.exists(json_path):
    print(f"Error: Could not find {json_path}")
    exit(1)

with open(json_path, "r", encoding="utf-8") as f:
    data = json.load(f)

albums_dict = data.get("albums", {})

# 1. Create Albums in Nextcloud
print("--- Creating Albums ---")
for album_name in albums_dict.keys():
    print(f"Creating album: '{album_name}'")
    cmd = [
        "docker",
        "exec",
        "--user",
        "www-data",
        "nextcloud-aio-nextcloud",
        "php",
        "occ",
        "photos:album:create",
        nc_user,
        album_name,
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# 2. Add Photos to Albums
print("\n--- Assigning Photos to Albums ---")
for album_name, file_list in albums_dict.items():
    print(f"Processing album: '{album_name}' ({len(file_list)} items)...")
    added_count = 0

    for item in file_list:
        filename = item.get("fileName") or os.path.basename(
            item.get("targetPath", "")
        )
        if not filename:
            continue

        photo_rel_path = f"{relative_base_path}/{filename}"
        photo_disk_path = f"/mnt/hdd/docker/nextcloud-data/{nc_user}/files/{photo_rel_path}"

        if os.path.exists(photo_disk_path):
            cmd = [
                "docker",
                "exec",
                "--user",
                "www-data",
                "nextcloud-aio-nextcloud",
                "php",
                "occ",
                "photos:album:add",
                nc_user,
                album_name,
                photo_rel_path,
            ]
            subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            added_count += 1

    print(f"  └─ Added {added_count} photos to '{album_name}'.")

print("\nFinished! All albums created and photos assigned successfully.")
