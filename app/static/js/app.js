document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('img[data-fallback-src]').forEach((image) => {
    image.addEventListener('error', () => {
      const fallbackSource = image.dataset.fallbackSrc;
      if (fallbackSource && image.src !== fallbackSource) {
        image.dataset.fallbackSrc = '';
        image.src = fallbackSource;
      } else {
        image.closest('.auth-visual')?.classList.add('image-unavailable');
      }
    });
  });

  const resetTokenInput = document.querySelector('[data-reset-token]');
  if (resetTokenInput && window.location.hash.length > 1) {
    resetTokenInput.value = window.location.hash.slice(1);
    window.history.replaceState(null, '', window.location.pathname);
  }

  document.querySelectorAll('.sidebar').forEach((sidebar) => {
    const toggle = sidebar.querySelector('.sidebar-toggle');
    const navigationLinks = sidebar.querySelectorAll('.sidebar-link[href^="#"]');
    const contentPanel = sidebar.closest('.dashboard')?.querySelector('.content-panel');

    if (toggle) {
      toggle.addEventListener('click', () => {
        const collapsed = sidebar.classList.toggle('is-collapsed');
        toggle.setAttribute('aria-expanded', String(!collapsed));
        toggle.setAttribute('aria-label', collapsed ? 'Expand navigation' : 'Collapse navigation');
        toggle.title = collapsed ? 'Expand navigation' : 'Collapse navigation';
      });
    }

    const updateActiveLink = () => {
      const currentHash = window.location.hash || navigationLinks[0]?.getAttribute('href') || '';
      navigationLinks.forEach((link) => {
        const isActive = link.getAttribute('href') === currentHash;
        link.classList.toggle('active', isActive);
        if (isActive) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      });

      if (!contentPanel) return;
      contentPanel.querySelectorAll('.dashboard-topic-hidden').forEach((element) => {
        element.classList.remove('dashboard-topic-hidden');
      });

      if (!window.location.hash || window.location.hash === '#overview') return;
      const target = document.getElementById(window.location.hash.slice(1));
      if (!target || !contentPanel.contains(target)) return;

      if (target.matches('[data-dashboard-topic]')) {
        Array.from(target.parentElement.children).forEach((sibling) => {
          if (sibling.matches('tbody[data-dashboard-topic]') && sibling !== target) {
            sibling.classList.add('dashboard-topic-hidden');
          }
        });
      }

      let topic = target.closest('.panel-card') || target;
      while (topic !== contentPanel) {
        const parent = topic.parentElement;
        if (!parent || !contentPanel.contains(parent)) break;
        Array.from(parent.children).forEach((sibling) => {
          if (sibling !== topic) sibling.classList.add('dashboard-topic-hidden');
        });
        topic = parent;
      }
    };

    updateActiveLink();
    window.addEventListener('hashchange', updateActiveLink);
  });

  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const qrRefreshTimers = new WeakMap();
  document.querySelectorAll('.generate-qr').forEach((button) => {
    const loadQr = async () => {
      const previousTimer = qrRefreshTimers.get(button);
      if (previousTimer) window.clearTimeout(previousTimer);
      const output = document.getElementById('qr-output');
      if (!output) return;
      output.textContent = 'Generating a short-lived QR code...';
      button.disabled = true;
      try {
        const csrfMeta = document.querySelector('meta[name="csrf-token"]');
        const response = await fetch(`/faculty/session/${button.dataset.sessionId}/qr`, {
          method: 'POST',
          headers: { 'X-CSRFToken': csrfMeta ? csrfMeta.content : '' }
        });
        const data = await response.json();
        if (!response.ok || !data.qr_image) {
          output.textContent = data.message || 'Unable to generate QR code.';
          return;
        }
        output.replaceChildren();
        const image = document.createElement('img');
        image.src = data.qr_image;
        image.alt = 'Short-lived attendance QR code';
        const status = document.createElement('span');
        status.textContent = 'QR refreshes automatically while this page remains open.';
        output.append(image, status);
        const delay = Math.max(Number(data.refresh_seconds) || 1, 1) * 1000;
        qrRefreshTimers.set(button, window.setTimeout(loadQr, delay));
      } catch {
        output.textContent = 'Unable to reach the QR service. Try again.';
      } finally {
        button.disabled = false;
      }
    };

    button.addEventListener('click', loadQr);
  });

  const video = document.getElementById('qr-video');
  const startBtn = document.getElementById('start-scan');
  const stopBtn = document.getElementById('stop-scan');
  const statusBox = document.getElementById('scan-status');

  if (!video || !startBtn || !stopBtn) return;

  let stream = null;
  let scanTimer = null;
  let scanInProgress = false;
  let cameraRequest = 0;

  const stopCamera = () => {
    cameraRequest += 1;
    if (stream) {
      stream.getTracks().forEach(track => track.stop());
      stream = null;
    }
    video.srcObject = null;
    if (scanTimer) {
      clearInterval(scanTimer);
      scanTimer = null;
    }
  };

  const submitToken = async (token) => {
    const tokenData = { token };
    const meta = document.querySelector('meta[name="csrf-token"]');
    for (let attempt = 0; attempt < 3; attempt += 1) {
      let response;
      try {
        response = await fetch('/student/submit-attendance', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': meta ? meta.content : ''
          },
          body: JSON.stringify(tokenData)
        });
      } catch {
        if (attempt < 2) {
          statusBox.textContent = 'Connection interrupted. Retrying attendance submission...';
          await new Promise(resolve => window.setTimeout(resolve, 500 * (attempt + 1)));
          continue;
        }
        statusBox.textContent = 'Unable to reach the server. Reconnect and scan the current QR code again.';
        return false;
      }

      if (response.status >= 500 && attempt < 2) {
        statusBox.textContent = 'Attendance service is temporarily unavailable. Retrying...';
        await new Promise(resolve => window.setTimeout(resolve, 500 * (attempt + 1)));
        continue;
      }

      let payload = {};
      try {
        payload = await response.json();
      } catch {
        payload = {};
      }
      statusBox.textContent = payload.message || 'Unable to process attendance. Scan the current QR code again.';
      return Boolean(response.ok && payload.ok);
    }
    statusBox.textContent = 'Unable to reach the server. Reconnect and scan the current QR code again.';
    return false;
  };

  const tick = async () => {
    if (scanInProgress || !video || video.readyState < 2) return;
    const canvas = document.getElementById('qr-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const code = window.jsQR ? window.jsQR(imageData.data, canvas.width, canvas.height) : null;
    if (code) {
      scanInProgress = true;
      stopCamera();
      statusBox.textContent = 'QR detected. Submitting attendance...';
      if (await submitToken(code.data)) {
        window.setTimeout(() => window.location.assign('/student/dashboard'), 900);
      }
      scanInProgress = false;
    }
  };

  startBtn.addEventListener('click', async () => {
    if (stream) return;
    if (!window.isSecureContext) {
      statusBox.textContent = 'Camera access requires HTTPS or localhost. Open this page using a secure connection and try again.';
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      statusBox.textContent = 'Camera scanning is not supported in this browser. Use a supported browser over HTTPS or localhost.';
      return;
    }
    if (typeof window.jsQR !== 'function') {
      statusBox.textContent = 'The QR scanner could not load. Check your internet connection, reload the page, and try again.';
      return;
    }

    const requestId = ++cameraRequest;
    startBtn.disabled = true;
    statusBox.textContent = 'Requesting camera access...';
    try {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: 'environment' } }
        });
      } catch (error) {
        if (error.name === 'NotAllowedError' || error.name === 'SecurityError') throw error;
        stream = await navigator.mediaDevices.getUserMedia({ video: true });
      }

      if (requestId !== cameraRequest) {
        stream.getTracks().forEach(track => track.stop());
        stream = null;
        return;
      }

      video.srcObject = stream;
      await video.play();
      if (requestId !== cameraRequest) {
        stopCamera();
        return;
      }
      statusBox.textContent = 'Camera active. Scan the class QR code.';
      scanTimer = setInterval(tick, 1000);
    } catch (error) {
      stopCamera();
      if (error.name === 'NotAllowedError' || error.name === 'SecurityError') {
        statusBox.textContent = 'Camera permission was denied or blocked. Allow camera access in browser settings and reload this page.';
      } else if (error.name === 'NotFoundError') {
        statusBox.textContent = 'No camera was found. Connect or enable a camera and try again.';
      } else if (error.name === 'NotReadableError') {
        statusBox.textContent = 'The camera is being used by another app. Close that app and try again.';
      } else if (error.name === 'AbortError') {
        statusBox.textContent = 'The camera could not start. Close other apps using the camera, then reload this page and try again.';
      } else if (error.name === 'OverconstrainedError') {
        statusBox.textContent = 'The camera rejected the requested settings. Try another camera or browser.';
      } else {
        const errorName = error.name || 'UnknownError';
        statusBox.textContent = `Camera unavailable (${errorName}). Check the browser and device camera permissions, close other apps using the camera, and try again.`;
      }
    } finally {
      startBtn.disabled = false;
    }
  });

  stopBtn.addEventListener('click', () => {
    stopCamera();
    statusBox.textContent = 'Camera stopped.';
  });

  window.addEventListener('pagehide', stopCamera);
});
