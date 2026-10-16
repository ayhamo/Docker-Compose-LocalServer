FROM lscr.io/linuxserver/webtop:ubuntu-xfce

# 1. Install Flatpak, xdotool, and desktop tools
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        flatpak \
        xdotool \
        desktop-file-utils && \
    chmod u+s /usr/bin/bwrap && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. Add Flathub and Install Sober + required GL/Mesa runtimes
RUN flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo && \
    flatpak install --noninteractive -y flathub org.vinegarhq.Sober && \
    flatpak install --noninteractive -y flathub org.freedesktop.Platform.GL.default//23.08 org.freedesktop.Platform.GL.default//24.08 || true

# 3. Grant Flatpak system permissions to access host GPU
RUN flatpak override --system --device=all org.vinegarhq.Sober && \
    flatpak override --system --filesystem=/dev/dri org.vinegarhq.Sober

# 4. Integrate into XFCE Applications Menu
RUN cp -f /var/lib/flatpak/exports/share/applications/org.vinegarhq.Sober.desktop /usr/share/applications/ && \
    update-desktop-database /usr/share/applications/
