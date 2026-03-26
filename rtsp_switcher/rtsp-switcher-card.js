class RtspSwitcherCard extends HTMLElement {
  set hass(hass) {
    if (this._hass) return;
    this._hass = hass;
    this._render(hass);
  }

  async _render(hass) {
    const height = (this._config && this._config.height) || 600;
    try {
      const result = await hass.callApi('GET', 'hassio/addons/rtsp_switcher/info');
      const url = result.data.ingress_url.replace(/\/?$/, '/') + 'embed';
      this.innerHTML = `<ha-card><iframe src="${url}" style="width:100%;height:${height}px;border:none;display:block;"></iframe></ha-card>`;
    } catch (e) {
      this.innerHTML = `<ha-card style="padding:16px;color:var(--error-color)">Could not load switcher: ${e.message}</ha-card>`;
    }
  }

  setConfig(config) {
    this._config = config;
  }

  getCardSize() {
    return 6;
  }
}

customElements.define('rtsp-switcher-card', RtspSwitcherCard);
