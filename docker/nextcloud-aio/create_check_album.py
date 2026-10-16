import os
import subprocess

# Configuration
txt_path = "/home/ayham/unassigned_photos.txt"
nc_user = "ayham"
album_name = "CHECK"
relative_base_path = "Media & Photos/Phone Photos"

if not os.path.exists(txt_path):
    print(f"Error: Could not find list file at: {txt_path}")
    print("Please upload unassigned_photos.txt to /home/ayham/ first.")
    exit(1)

# 1. Create the CHECK album
print(f"--- Creating Album: '{album_name}' ---")
cmd_create = [
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
subprocess.run(
    cmd_create, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)

# 2. Read the list of unassigned photos
with open(txt_path, "r", encoding="utf-8") as f:
    photos = [line.strip() for line in f if line.strip()]

print(f"Found {len(photos)} photos in list. Assigning to '{album_name}'...\n")

added_count = 0
for idx, filename in enumerate(photos, 1):
    photo_rel_path = f"{relative_base_path}/{filename}"
    photo_disk_path = (
        f"/mnt/hdd/docker/nextcloud-data/{nc_user}/files/{photo_rel_path}"
    )

    # Check if file exists on disk inside Nextcloud
    if os.path.exists(photo_disk_path):
        cmd_add = [
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
            cmd_add, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        added_count += 1

        # Show live progress every 25 photos
        if idx % 25 == 0 or idx == len(photos):
            print(f"  Progress: {idx}/{len(photos)} photos processed...")
    else:
        print(f"  [Warning] File not found on disk: {filename}")

print(
    f"\nFinished! Added {added_count}/{len(photos)} photos to album '{album_name}'."
)
