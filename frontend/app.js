/**
 * Integrated TOMV3 + Banco de Ensayo — Frontend Logic
 * Merges tomv3-web/app.js and BancoEnsayo-Web/app.js
 */
"use strict";

// ── WebSocket ──────────────────────────────────────────────────────────────
let ws = null;
let reconnectAttempts = 0;
const MAX_RECONNECT = 10;

// ── Application state ──────────────────────────────────────────────────────
let isSerialConnected  = false;
let isESPconnected      = false;
let isExperimentRunning = false;
let isManualExperimentRunning = false;
let _integratedStartPending = false;
let applyingConfig          = false;   // suppress cascade during bulk-apply
let _intentionalEspDisconnect = false;  // distinguish user-initiated from unexpected
let _positionCheckModalId = null;
let _timerPauseDepth = 0;               // >0 while timers are frozen (error pause / position check)
let _experimentPaused = false;          // experiment paused on motor/BLE error (banner shown)
let _lastPauseMsg = null;               // last experiment_pause payload for banner refresh
let _pausedPhaseUpdate = null;

// ── Max positions (set from server state_sync; fallback 12) ───────────────
let _maxPositions = 12;

// ── Position table references (built dynamically) ─────────────────────────
let _logAutoScrollEnabled = true;
const LOG_NEAR_BOTTOM_PX = 40;
const checkboxes       = [];
const sampleNameInputs = [];

// ── Auto-save debounce ─────────────────────────────────────────────────────
let _saveTimer = null;
// ── Client-side folder handle (File System Access API) ────────────────────
let dirHandle = null;

// ── Web Audio notifications (browser-local) ────────────────────────────────
let _audioCtx = null;
let _audioUnlocked = false;
let _lastToneAtMs = 0;
const TONE_MIN_INTERVAL_MS = 500;

function ensureAudioContext() {
    if (_audioCtx) return _audioCtx;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    _audioCtx = new Ctx();
    return _audioCtx;
}

async function unlockNotificationAudio() {
    const ctx = ensureAudioContext();
    if (!ctx) return false;
    try {
        if (ctx.state === 'suspended') {
            await ctx.resume();
        }
        _audioUnlocked = ctx.state === 'running';
        return _audioUnlocked;
    } catch (_e) {
        return false;
    }
}

function playNotificationTone(kind = 'info') {
    const nowMs = Date.now();
    if (nowMs - _lastToneAtMs < TONE_MIN_INTERVAL_MS) return;

    const ctx = ensureAudioContext();
    if (!ctx || ctx.state !== 'running') return;

    const profiles = {
        success: { frequency: 880, duration: 1.5, gain: 0.09 },
        warning: { frequency: 1200, duration: 1.5, gain: 0.09 },
        error:   { frequency: 380, duration: 1.5, gain: 0.10 },
        info:    { frequency: 520, duration: 1.5, gain: 0.07 },
    };
    const profile = profiles[kind] || profiles.info;

    try {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = kind === 'error' ? 'square' : 'sine';
        osc.frequency.value = profile.frequency;

        const t0 = ctx.currentTime;
        gain.gain.setValueAtTime(0.0001, t0);
        gain.gain.exponentialRampToValueAtTime(profile.gain, t0 + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + profile.duration);

        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(t0);
        osc.stop(t0 + profile.duration);
        _lastToneAtMs = nowMs;
    } catch (_e) {
        // Keep UI flow uninterrupted if audio playback fails.
    }
}
// ══════════════════════════════════════════════════════════════════════════
//  INIT
// ══════════════════════════════════════════════════════════════════════════

function init() {
    buildPositionTable();
    setupEventListeners();
    connectWebSocket();
    setTimeout(refreshPorts, 500);
    updateAcqHint();
}

// ══════════════════════════════════════════════════════════════════════════
//  WEBSOCKET
// ══════════════════════════════════════════════════════════════════════════

function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${location.host}/ws`;
    updateConnectionStatus('connecting');
    ws = new WebSocket(url);

    ws.onopen = () => {
        updateConnectionStatus('connected');
        reconnectAttempts = 0;
        appendLog('WebSocket conectado al servidor', 'success');
    };

    ws.onmessage = (event) => {
        try { handleMessage(JSON.parse(event.data)); }
        catch (e) { console.error('WS parse error:', e); }
    };

    ws.onerror = () => updateConnectionStatus('error');

    ws.onclose = () => {
        updateConnectionStatus('disconnected');
        if (reconnectAttempts < MAX_RECONNECT) {
            const delay = 1000 * Math.pow(1.5, reconnectAttempts++);
            setTimeout(connectWebSocket, delay);
        } else {
            showAlert('Conexión perdida. Recarga la página.', 'danger');
        }
    };
}

function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(obj));
    } else {
        showAlert('No conectado al servidor', 'warning');
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  MESSAGE HANDLER
// ══════════════════════════════════════════════════════════════════════════

function handleMessage(msg) {
    switch (msg.type) {

        case 'connected':
            appendLog(msg.message || 'Conectado', 'success');
            break;

        case 'state_sync':
            syncState(msg.state);
            break;

        case 'data':
            if (typeof handlePlotData === 'function') handlePlotData(msg.plot_data);
            break;

        case 'log':
            appendLog(msg.message);
            break;

        case 'log_cleared':
            clearLogTerminal();
            break;

        case 'timer_start':
            if (isManualExperimentRunning || isManualPhase(msg.phase)) {
                updateManualTimerPhase(msg.description || msg.phase);
            } else {
                startTimerDisplay(msg.phase, msg.total_seconds, msg.description);
            }
            break;

        case 'timer_update':
            if (isManualExperimentRunning || isManualPhase(msg.phase)) {
                updateManualTimerPhase(msg.phase);
            } else {
                if (_timerPauseDepth > 0) {
                    _pausedPhaseUpdate = msg;
                } else {
                    updateTimerDisplay(msg.remaining, msg.total, msg.phase);
                }
            }
            break;

        case 'elapsed_time':
            // secondary elapsed seconds — kept for compatibility, ignored visually
            break;

        case 'experiment_start':
            startGlobalTimer(msg.total_seconds);
            break;

        case 'experiment_complete':
            _integratedStartPending = false;
            closePositionCheckModal();
            handleExperimentComplete(msg);
            break;

        case 'manual_experiment_start':
            setManualExperimentRunning(true);
            document.getElementById('global-timer-container').style.display = 'none';
            document.getElementById('timer-container').style.display = 'none';
            startManualTimer(msg.total_seconds || 0);
            appendLog(`Experimento manual iniciado: ${msg.sample_name || 'Manual'}`);
            break;

        case 'manual_experiment_complete':
            handleManualExperimentComplete(msg);
            break;

        case 'experiment_status':
            const statusEl = document.getElementById('experimentStatusDisplay');
            if (statusEl) statusEl.textContent = msg.message || '—';
            // Re-enable UI if terminal state
            const lower = (msg.message || '').toLowerCase();
            if (lower.includes('complet') || lower.includes('cancel') || lower.includes('error')) {
                setExperimentRunning(false);
            }
            break;

        case 'position':
            document.getElementById('positionDisplay').textContent = `${msg.value}/${msg.total_positions || 5}`;
            if (msg.cycle !== undefined)
                document.getElementById('cycleDisplay').textContent = `${msg.cycle}/${msg.cycles_total}`;
            if (msg.sample_name !== undefined)
                document.getElementById('sampleDisplay').textContent = msg.sample_name || '—';
            break;

        case 'connection_status':
            // ESP32 BLE connection
            setEspStatus(msg.message, msg.connected);
            break;

        case 'serial_status':
            setSerialStatus(msg.message, msg.connected);
            break;

        case 'position_check_required':
            handlePositionCheckRequired(msg);
            break;

        case 'position_check_cleared':
            closePositionCheckModal();
            break;

        case 'experiment_pause':
            handleExperimentPause(msg);
            break;

        case 'experiment_resumed':
            handleExperimentResumed();
            break;

        case 'serial_disconnected':
            handleSerialDisconnection(msg);
            break;

        case 'auto_save_result':
            if (msg.success) {
                appendLog(`✓ CSV listo: ${msg.filename}  (${msg.rows} filas) — descargando…`, 'success');
                downloadAutoSave(msg.filename, msg.download_url);
            } else {
                appendLog(`✗ Error al guardar CSV: ${msg.message}`, 'error');
            }
            break;

        case 'config_saved':
            setConfigStatus('guardado ✓', 'success');
            setTimeout(() => setConfigStatus('al día', 'secondary'), 2000);
            break;

        case 'config_save_error':
            setConfigStatus('error al guardar!', 'danger');
            appendLog(`Error al guardar config: ${msg.message}`, 'error');
            break;

        case 'error':
            _integratedStartPending = false;
            appendLog(`ERROR: ${msg.message}`, 'error');
            showAlert(msg.message, 'danger');
            break;

        default:
            console.log('Unknown message type:', msg.type);
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  STATE SYNC
// ══════════════════════════════════════════════════════════════════════════

function syncState(state) {
    if (!state) return;
    applyingConfig = true;
    try {
        // Max positions from server (single source of truth)
        if (state.max_positions) _maxPositions = state.max_positions;

        // TOMV3 params
        if (state.experiment_params) {
            const p = state.experiment_params;
            setVal('param-tmin',   p.TMin);
            setVal('param-tmax',   p.TMax);
            setVal('param-vmin',   p.VMin);
            setVal('param-vmax',   p.VMax);
            setVal('param-steps',  p.Steps);
            setVal('param-cycles', p.Cycles);
        }
        if (state.sweep_type) {
            document.getElementById('sweep-type').value = state.sweep_type;
        }
        if (state.serial_connected) {
            const serialDeviceId = (state.serial_device_id || '').trim();
            const serialMessage = serialDeviceId ? `Connected to ${serialDeviceId}` : 'Connected';
            setSerialStatus(serialMessage, true);
        } else {
            setSerialStatus('Disconnected', false);
        }

        // Banco params
        if (state.desorption_time !== undefined) setVal('desorptionTime', state.desorption_time);
        if (state.pre_conditioning_time !== undefined) setVal('preCondTime', state.pre_conditioning_time);
        if (state.cycle_gap_time !== undefined)  setVal('cycleGapTime', state.cycle_gap_time);
        if (state.cycles !== undefined)          setVal('experimentCycles', state.cycles);
        if (state.ble_address !== undefined)     setVal('ble-address', state.ble_address);

        if (Array.isArray(state.positions)) {
            state.positions.forEach((pos, i) => {
                if (i >= checkboxes.length) return;
                checkboxes[i].checked = !!pos.enabled;
                sampleNameInputs[i].disabled = !pos.enabled;
                sampleNameInputs[i].value    = pos.name || '';
            });
        }

        if (state.ble_connected) {
            setEspStatus('Conectado', true);
        }
        if (state.experiment_running && state.experiment_mode === 'manual') {
            _integratedStartPending = false;
            setExperimentRunning(false);
            setManualExperimentRunning(true);
        }

        if (state.experiment_running && state.experiment_mode !== 'manual') {
            _integratedStartPending = false;
            setManualExperimentRunning(false);
            setExperimentRunning(true);
            // Restore mid-experiment displays for reconnecting clients
            if (state.experiment_state) {
                const es = state.experiment_state;
                if (es.current_position)
                    document.getElementById('positionDisplay').textContent =
                        `${es.current_position}/${es.total_positions}`;
                if (es.current_cycle)
                    document.getElementById('cycleDisplay').textContent =
                        `${es.current_cycle}/${es.cycles_total}`;
                if (es.current_sample)
                    document.getElementById('sampleDisplay').textContent =
                        es.current_sample || '—';
                if (es.total_experiment_seconds > 0)
                    startGlobalTimer(es.total_experiment_seconds, es.experiment_elapsed_seconds);
                if (es.current_phase && es.current_phase_total > 0) {
                    startTimerDisplay(es.current_phase, es.current_phase_total, es.current_phase_description);
                    updateTimerDisplay(es.current_phase_remaining, es.current_phase_total, es.current_phase);
                }
                if (es.position_check_pending) {
                    handlePositionCheckRequired({
                        position: es.position_check_position,
                        sample_name: es.position_check_sample,
                        cycle: es.position_check_cycle,
                        cycles_total: es.position_check_cycles_total,
                        response: es.position_check_response,
                        message: 'Verifica la posicion de la muestra antes de continuar.',
                    });
                }
                if (es.paused) {
                    handleExperimentPause({
                        reasons: es.pause_reasons || [],
                        ble_connected: !!es.ble_connected,
                    });
                }
            }
        }

        if (!state.experiment_running && !(_integratedStartPending && isExperimentRunning)) {
            _integratedStartPending = false;
            setManualExperimentRunning(false);
            setExperimentRunning(false);
        }
    } finally {
        applyingConfig = false;
        updateAcqHint();
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  EVENT LISTENERS
// ══════════════════════════════════════════════════════════════════════════

function setupEventListeners() {
    // Unlock browser audio once after first interaction (autoplay policy).
    document.addEventListener('pointerdown', () => { void unlockNotificationAudio(); }, { once: true, passive: true });
    document.addEventListener('keydown', () => { void unlockNotificationAudio(); }, { once: true });

    // Serial
    document.getElementById('btn-refresh-ports').addEventListener('click', refreshPorts);
    document.getElementById('btn-connect').addEventListener('click', connectSerial);
    document.getElementById('btn-disconnect').addEventListener('click', disconnectSerial);

    // Params
    document.getElementById('btn-send-params').addEventListener('click', sendParameters);
    document.getElementById('sweep-type').addEventListener('change', sendSweepType);

    // Param change → update acq hint
    ['param-steps', 'param-cycles'].forEach(id => {
        document.getElementById(id).addEventListener('input', updateAcqHint);
    });
    document.getElementById('desorptionTime').addEventListener('input', updateAcqHint);
    document.getElementById('experimentCycles').addEventListener('input', updateAcqHint);
    document.getElementById('preCondTime').addEventListener('input', updateAcqHint);
    document.getElementById('cycleGapTime').addEventListener('input', updateAcqHint);

    // TOMV3 params → auto-save on change (persist without needing Enviar)
    ['param-tmin', 'param-tmax', 'param-vmin', 'param-vmax', 'param-steps', 'param-cycles'].forEach(id => {
        document.getElementById(id).addEventListener('input', scheduleAutoSave);
    });
    document.getElementById('sweep-type').addEventListener('change', scheduleAutoSave);

    // Experiment
    document.getElementById('btn-start-exper').addEventListener('click', startExperiment);
    document.getElementById('btn-stop-exper').addEventListener('click', cancelExperiment);
    document.getElementById('btn-start-manual-exper').addEventListener('click', startManualExperiment);
    document.getElementById('btn-stop-manual-exper').addEventListener('click', cancelManualExperiment);
    document.getElementById('btn-export').addEventListener('click', exportData);

    // Folder picker
    const pickBtn = document.getElementById('btn-pick-folder');
    if (pickBtn) pickBtn.addEventListener('click', pickSaveFolder);

    // Log
    document.getElementById('btn-clear-log').addEventListener('click', () => {
        send({ type: 'clear_log' });
    });
    const logTerminal = document.getElementById('log-terminal');
    if (logTerminal) {
        logTerminal.addEventListener('scroll', () => {
            _logAutoScrollEnabled = isLogNearBottom(logTerminal);
        }, { passive: true });
    }

    // ESP32
    document.getElementById('connectEspBtn').addEventListener('click', toggleEspConnection);

    // Banco params → auto-save
    ['desorptionTime', 'experimentCycles', 'preCondTime', 'cycleGapTime'].forEach(id => {
        document.getElementById(id).addEventListener('input', scheduleAutoSave);
    });
    const bleAddressInput = document.getElementById('ble-address');
    if (bleAddressInput) {
        bleAddressInput.addEventListener('input', scheduleAutoSave);
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  SERIAL (TOMV3)
// ══════════════════════════════════════════════════════════════════════════

async function refreshPorts() {
    try {
        const res  = await fetch('/api/ports');
        const data = await res.json();
        const sel  = document.getElementById('port-select');
        sel.innerHTML = '<option value="">Seleccionar puerto…</option>';
        (data.ports || []).forEach(p => {
            const opt = document.createElement('option');
            opt.value = opt.textContent = p;
            sel.appendChild(opt);
        });
        appendLog(`${data.ports.length} puerto(s) encontrado(s)`);
    } catch (e) {
        appendLog(`Error refrescando puertos: ${e.message}`, 'error');
    }
}

function connectSerial() {
    const port = document.getElementById('port-select').value;
    if (!port) { showAlert('Selecciona un puerto COM', 'warning'); return; }
    if (!(ws && ws.readyState === WebSocket.OPEN)) {
        showAlert('No conectado al servidor', 'warning');
        return;
    }

    const connectBtn = document.getElementById('btn-connect');
    connectBtn.disabled = true;
    connectBtn.innerHTML = '<i class="bi bi-hourglass-split"></i> Conectando…';

    send({ type: 'connect_serial', port });
}

function disconnectSerial() {
    send({ type: 'disconnect_serial' });
    setSerialStatus('Disconnected', false);
}

function setSerialStatus(message, connected) {
    isSerialConnected = !!connected;

    const badge = document.getElementById('serialStatus');
    const connectBtn = document.getElementById('btn-connect');
    const disconnectBtn = document.getElementById('btn-disconnect');

    if (badge) {
        if (connected) {
            badge.className = 'badge bg-success ms-1';
            badge.innerHTML = `<i class="bi bi-check-circle-fill"></i> ${message || 'Connected'}`;
        } else {
            badge.className = 'badge bg-danger ms-1';
            badge.innerHTML = `<i class="bi bi-x-circle-fill"></i> ${message || 'Disconnected'}`;
        }
    }

    if (connectBtn && disconnectBtn) {
        connectBtn.disabled = false;
        connectBtn.innerHTML = '<i class="bi bi-plug"></i> Conectar';
        connectBtn.style.display = connected ? 'none' : 'block';
        disconnectBtn.style.display = connected ? 'block' : 'none';
    }
}

function sendParameters() {
    send({
        type:   'config_params',
        TMin:   parseFloat(document.getElementById('param-tmin').value),
        TMax:   parseFloat(document.getElementById('param-tmax').value),
        VMin:   parseFloat(document.getElementById('param-vmin').value),
        VMax:   parseFloat(document.getElementById('param-vmax').value),
        Steps:  parseInt(document.getElementById('param-steps').value),
        Cycles: parseInt(document.getElementById('param-cycles').value),
    });
}

function sendSweepType() {
    send({ type: 'sweep_type', mode: document.getElementById('sweep-type').value });
}

async function exportData() {
    try {
        const res  = await fetch('/api/export', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            const a    = document.createElement('a');
            a.href     = data.download_url;
            a.download = data.filename;
            a.click();
            showAlert(`Exportado: ${data.filename}  (${data.rows} filas)`, 'success');
        } else {
            showAlert('Error en la exportación', 'danger');
        }
    } catch (e) {
        appendLog(`Export error: ${e.message}`, 'error');
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  CLIENT-SIDE FOLDER PICKER & AUTO-SAVE DOWNLOAD
// ══════════════════════════════════════════════════════════════════════════

async function pickSaveFolder() {
    if (!('showDirectoryPicker' in window)) {
        showAlert('Tu navegador no soporta selección de carpeta (File System Access API). Los CSV se descargarán en la carpeta de Descargas del navegador.', 'warning');
        return;
    }
    try {
        dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
        document.getElementById('saveFolderDisplay').textContent = dirHandle.name;
        document.getElementById('saveFolderDisplay').classList.remove('fst-italic', 'text-muted');
        document.getElementById('saveFolderDisplay').classList.add('text-success', 'fw-semibold');
        appendLog(`✓ Carpeta de destino seleccionada: ${dirHandle.name}`, 'success');
    } catch (e) {
        if (e.name !== 'AbortError') {
            appendLog(`Error al seleccionar carpeta: ${e.message}`, 'error');
        }
    }
}

async function downloadAutoSave(filename, url) {
    try {
        const res  = await fetch(url);
        const blob = await res.blob();

        if (dirHandle) {
            // Write directly into the user-selected folder — no dialog
            const fileHandle = await dirHandle.getFileHandle(filename, { create: true });
            const writable   = await fileHandle.createWritable();
            await writable.write(blob);
            await writable.close();
            appendLog(`✓ CSV guardado en "${dirHandle.name}/${filename}"`, 'success');
        } else {
            // Fallback: standard browser download
            const a    = document.createElement('a');
            a.href     = URL.createObjectURL(blob);
            a.download = filename;
            a.click();
            URL.revokeObjectURL(a.href);
            appendLog(`✓ CSV descargado: ${filename}`, 'success');
        }
    } catch (e) {
        appendLog(`✗ Error al descargar CSV: ${e.message}`, 'error');
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  ESP32 (BANCO DE ENSAYO)
// ══════════════════════════════════════════════════════════════════════════

function connectEsp() {
    const macInput = document.getElementById('ble-address');
    const rawMac = (macInput?.value || '').trim();
    const normalizedMac = normalizeMacAddress(rawMac);

    if (rawMac && !normalizedMac) {
        showAlert('MAC inválida. Usa el formato AA:BB:CC:DD:EE:FF', 'warning');
        return Promise.resolve(false);
    }

    if (macInput && normalizedMac) {
        macInput.value = normalizedMac;
    }

    document.getElementById('connectEspBtn').disabled = true;
    document.getElementById('connectEspBtn').textContent = 'Escaneando…';
    send({ type: 'connect_ble', address: normalizedMac || '' });
    return Promise.resolve(true);
}

function toggleEspConnection() {
    if (isESPconnected) {
        _intentionalEspDisconnect = true;
        send({ type: 'disconnect_ble' });
    } else {
        connectEsp();
    }
}

function setEspStatus(message, connected) {
    const wasConnected = isESPconnected;
    isESPconnected = connected;
    const badge = document.getElementById('espStatus');
    const btn   = document.getElementById('connectEspBtn');

    if (connected) {
        badge.className      = 'badge bg-success';
        badge.innerHTML      = `<i class="bi bi-check-circle-fill"></i> ${message}`;
        btn.textContent      = 'Desconectar';
        btn.className        = 'btn btn-outline-danger btn-sm';
        btn.disabled         = false;
    } else {
        badge.className      = 'badge bg-danger';
        badge.innerHTML      = `<i class="bi bi-x-circle-fill"></i> ${message}`;
        btn.textContent      = 'Conectar';
        btn.className        = 'btn btn-primary btn-sm';
        btn.disabled         = false;

        // Show modal only on unexpected disconnection (not user-initiated).
        // While an integrated experiment is running the pause banner already
        // informs the user — an extra modal would be redundant.
        if (wasConnected && !_intentionalEspDisconnect && !isExperimentRunning) {
            playNotificationTone('error');
            appendLog(`⚠ ESP32 DESCONECTADO: ${message}`, 'error');
            showModal(
                'bg-danger text-white',
                '<i class="bi bi-exclamation-triangle-fill"></i> ESP32 Desconectado',
                `<p><strong>Se perdió la conexión BLE con el ESP32 inesperadamente.</strong></p><p>${message}</p><p>Asegúrate de que el ESP32 esté encendido y vuelve a conectar.</p>`
            );
        }
        _intentionalEspDisconnect = false;
    }

    // Keep the pause banner's BLE state in sync (e.g. reconnect attempt
    // succeeded or failed while the experiment is paused).
    if (_experimentPaused && _lastPauseMsg) {
        _renderPauseBanner({ ..._lastPauseMsg, ble_connected: isESPconnected });
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  INTEGRATED EXPERIMENT
// ══════════════════════════════════════════════════════════════════════════

function startExperiment() {
    if (isManualExperimentRunning) {
        showAlert('Hay un experimento manual en ejecución', 'warning');
        return;
    }

    if (!isSerialConnected) {
        showAlert('Conecta primero el puerto serial (TOMV3)', 'warning');
        return;
    }
    if (!isESPconnected

    ) {
        showAlert('Conecta primero el ESP32 (Banco de Ensayo)', 'warning');
        return;
    }

    const desorptionTime = parseFloat(document.getElementById('desorptionTime').value);
    const cycles         = parseInt(document.getElementById('experimentCycles').value, 10);

    if (isNaN(desorptionTime) || desorptionTime < 0) {
        appendLog('Error: tiempo de desorción inválido', 'error'); return;
    }
    if (isNaN(cycles) || cycles < 1) {
        appendLog('Error: número de ciclos inválido', 'error'); return;
    }

    const enabledPositions = [];
    checkboxes.forEach((cb, i) => { if (cb.checked) enabledPositions.push(i + 1); });
    if (enabledPositions.length === 0) {
        appendLog('Error: activa al menos una posición', 'error'); return;
    }

    const sampleNames = sampleNameInputs.map(inp => inp.value.trim());

    _integratedStartPending = true;
    setExperimentRunning(true);
    send({
        type:                  'start_experiment',
        sample_names:          sampleNames,
        enabled_positions:     enabledPositions,
        desorption_time:       desorptionTime,
        cycle_gap_time:        parseFloat(document.getElementById('cycleGapTime').value) || 0,
        cycles:                cycles,
        pre_conditioning_time: parseFloat(document.getElementById('preCondTime').value) || 300,
    });
}

function startManualExperiment() {
    if (isExperimentRunning) {
        showAlert('Hay un experimento integrado en ejecución', 'warning');
        return;
    }
    if (!isSerialConnected) {
        showAlert('Conecta primero el puerto serial (TOMV3)', 'warning');
        return;
    }

    const sampleName = (document.getElementById('manual-sample-name').value || '').trim();
    if (!sampleName) {
        showAlert('Ingresa un nombre de muestra para el experimento manual', 'warning');
        return;
    }

    send({
        type: 'start_manual_experiment',
        sample_name: sampleName,
        pre_conditioning_time: parseFloat(document.getElementById('preCondTime').value) || 300,
    });
}

function cancelExperiment() {
    send({ type: 'cancel_experiment' });
    document.getElementById('btn-stop-exper').disabled = true;
}

function cancelManualExperiment() {
    send({ type: 'cancel_experiment' });
    const manualStopBtn = document.getElementById('btn-stop-manual-exper');
    if (manualStopBtn) {
        manualStopBtn.disabled = true;
    }
}

function setExperimentRunning(running) {
    isExperimentRunning = running;
    const startBtn = document.getElementById('btn-start-exper');
    const stopBtn  = document.getElementById('btn-stop-exper');
    const card     = document.getElementById('experimentCard');
    const manualBtn = document.getElementById('btn-start-manual-exper');

    if (running) {
        startBtn.style.display = 'none';
        stopBtn.style.display  = 'block';
        stopBtn.disabled       = false;
        card.classList.add('experiment-running');
        if (manualBtn) manualBtn.disabled = true;
    } else {
        closePositionCheckModal();
        _experimentPaused = false;
        _lastPauseMsg = null;
        _timerPauseDepth = 0;
        _pausedPhaseUpdate = null;
        _clearPauseBanner();
        startBtn.style.display = 'block';
        stopBtn.style.display  = 'none';
        startBtn.disabled      = isManualExperimentRunning;
        card.classList.remove('experiment-running');
        document.getElementById('positionDisplay').textContent = 'N/A';
        document.getElementById('cycleDisplay').textContent    = 'N/A';
        document.getElementById('sampleDisplay').textContent   = '—';
        document.getElementById('timer-container').style.display = 'none';
        stopGlobalTimer();
        if (manualBtn) manualBtn.disabled = false;
        _integratedStartPending = false;
    }
}

function setManualExperimentRunning(running) {
    isManualExperimentRunning = running;
    const manualBtn = document.getElementById('btn-start-manual-exper');
    const manualStopBtn = document.getElementById('btn-stop-manual-exper');
    const manualCard = document.getElementById('manualExperimentCard');
    const integratedStartBtn = document.getElementById('btn-start-exper');

    if (manualBtn) {
        manualBtn.style.display = running ? 'none' : 'block';
        manualBtn.disabled = false;
    }

    if (manualStopBtn) {
        manualStopBtn.style.display = running ? 'block' : 'none';
        manualStopBtn.disabled = false;
    }

    if (manualCard) {
        manualCard.classList.toggle('experiment-running', running);
    }

    if (integratedStartBtn) {
        integratedStartBtn.disabled = running;
    }

    if (!running) {
        stopManualTimer();
        document.getElementById('global-timer-container').style.display = 'none';
        document.getElementById('timer-container').style.display = 'none';
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  TIMER
// ══════════════════════════════════════════════════════════════════════════

function startTimerDisplay(phase, totalSeconds, description) {
    document.getElementById('timer-container').style.display = 'block';
    document.getElementById('timer-phase').textContent = description || phase;
}

function updateTimerDisplay(remaining, total, phase) {
    const m   = Math.floor(remaining / 60);
    const s   = remaining % 60;
    const str = `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    document.getElementById('timer-text').textContent = `Restante: ${str}`;
    const pct = total > 0 ? ((total - remaining) / total) * 100 : 0;
    document.getElementById('timer-progress').style.width = `${pct}%`;
}

// ══════════════════════════════════════════════════════════════════════════//  GLOBAL EXPERIMENT TIMER
// ══════════════════════════════════════════════════════════════════════════════

let _manualTimer = {
    total: 0,
    elapsed: 0,
    interval: null,
    startMs: 0,
    phaseLabel: 'Experimento manual',
};

function isManualPhase(phase) {
    return typeof phase === 'string' && phase.startsWith('manual_');
}

function syncManualElapsedFromClock() {
    if (_manualTimer.startMs <= 0) return;
    const elapsed = Math.floor((Date.now() - _manualTimer.startMs) / 1000);
    _manualTimer.elapsed = Math.max(0, Math.min(elapsed, _manualTimer.total));
}

function updateManualTimerDisplay() {
    const container = document.getElementById('manual-timer-container');
    const textEl = document.getElementById('manual-timer-text');
    const barEl = document.getElementById('manual-timer-progress');
    if (!container || !textEl || !barEl) return;

    const remaining = Math.max(_manualTimer.total - _manualTimer.elapsed, 0);
    const m = Math.floor(remaining / 60);
    const s = remaining % 60;
    textEl.textContent = `Restante: ${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    const pct = _manualTimer.total > 0 ? (_manualTimer.elapsed / _manualTimer.total) * 100 : 0;
    barEl.style.width = `${Math.min(pct, 100)}%`;
}

function startManualTimerInterval() {
    if (_manualTimer.interval) return;
    _manualTimer.interval = setInterval(() => {
        syncManualElapsedFromClock();
        updateManualTimerDisplay();
        if (_manualTimer.elapsed >= _manualTimer.total) {
            stopManualTimer(false);
        }
    }, 250);
}

function startManualTimer(totalSeconds) {
    stopManualTimer(false);
    _manualTimer.total = Math.max(0, Number(totalSeconds) || 0);
    _manualTimer.elapsed = 0;
    _manualTimer.startMs = Date.now();

    const container = document.getElementById('manual-timer-container');
    if (container) container.style.display = _manualTimer.total > 0 ? 'block' : 'none';

    updateManualTimerDisplay();
    if (_manualTimer.total > 0) {
        startManualTimerInterval();
    }
}

function updateManualTimerPhase(label) {
    if (!label) return;
    _manualTimer.phaseLabel = label;
}

function stopManualTimer(hide = true) {
    if (_manualTimer.interval) {
        clearInterval(_manualTimer.interval);
        _manualTimer.interval = null;
    }
    _manualTimer.startMs = 0;
    if (hide) {
        _manualTimer.total = 0;
        _manualTimer.elapsed = 0;
        const container = document.getElementById('manual-timer-container');
        const barEl = document.getElementById('manual-timer-progress');
        const textEl = document.getElementById('manual-timer-text');
        if (container) container.style.display = 'none';
        if (barEl) barEl.style.width = '0%';
        if (textEl) textEl.textContent = '--:--';
    }
}

let _globalTimer = {
    total: 0,
    elapsed: 0,
    interval: null,
    startMs: 0,
    pausedAtMs: null,
};

function syncGlobalElapsedFromClock() {
    if (_globalTimer.startMs <= 0) return;
    const nowMs = Date.now();
    const elapsed = Math.floor((nowMs - _globalTimer.startMs) / 1000);
    _globalTimer.elapsed = Math.max(0, Math.min(elapsed, _globalTimer.total));
}

function startGlobalTimerInterval() {
    if (_globalTimer.interval) return;
    _globalTimer.interval = setInterval(() => {
        syncGlobalElapsedFromClock();
        updateGlobalTimerDisplay();
        if (_globalTimer.elapsed >= _globalTimer.total) stopGlobalTimer();
    }, 250);
}

function startGlobalTimer(totalSeconds, initialElapsed = 0) {
    stopGlobalTimer();
    _globalTimer.total = Math.max(0, Number(totalSeconds) || 0);
    _globalTimer.elapsed = Math.max(0, Number(initialElapsed) || 0);
    _globalTimer.startMs = Date.now() - (_globalTimer.elapsed * 1000);
    _globalTimer.pausedAtMs = null;
    document.getElementById('global-timer-container').style.display = 'block';
    updateGlobalTimerDisplay();
    startGlobalTimerInterval();
}

function updateGlobalTimerDisplay() {
    const remaining = Math.max(_globalTimer.total - _globalTimer.elapsed, 0);
    const m   = Math.floor(remaining / 60);
    const s   = remaining % 60;
    document.getElementById('global-timer-text').textContent =
        `Restante: ${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    const pct = _globalTimer.total > 0
        ? (_globalTimer.elapsed / _globalTimer.total) * 100
        : 0;
    document.getElementById('global-timer-progress').style.width = `${Math.min(pct, 100)}%`;
}

function stopGlobalTimer() {
    if (_globalTimer.interval) {
        clearInterval(_globalTimer.interval);
        _globalTimer.interval = null;
    }
    _globalTimer.startMs = 0;
    _globalTimer.pausedAtMs = null;
    const container = document.getElementById('global-timer-container');
    if (container) container.style.display = 'none';
}

function pauseTimerVisuals() {
    _timerPauseDepth++;
    if (_timerPauseDepth > 1) return;  // already paused by another source
    syncGlobalElapsedFromClock();
    _globalTimer.pausedAtMs = Date.now();
    if (_globalTimer.interval) {
        clearInterval(_globalTimer.interval);
        _globalTimer.interval = null;
    }
    updateGlobalTimerDisplay();
}

function resumeTimerVisuals() {
    if (_timerPauseDepth === 0) return;
    _timerPauseDepth--;
    if (_timerPauseDepth > 0) return;  // still paused by another source

    if (_globalTimer.pausedAtMs && _globalTimer.startMs > 0) {
        const pausedDurationMs = Date.now() - _globalTimer.pausedAtMs;
        _globalTimer.startMs += pausedDurationMs;
    }
    _globalTimer.pausedAtMs = null;

    syncGlobalElapsedFromClock();
    updateGlobalTimerDisplay();

    if (_globalTimer.total > 0 && _globalTimer.elapsed < _globalTimer.total) {
        startGlobalTimerInterval();
    }

    if (_pausedPhaseUpdate) {
        updateTimerDisplay(_pausedPhaseUpdate.remaining, _pausedPhaseUpdate.total, _pausedPhaseUpdate.phase);
        _pausedPhaseUpdate = null;
    }
}

function handlePositionCheckRequired(msg) {
    if (!msg) return;

    // Ensure a single blocking modal instance.
    closePositionCheckModal();
    pauseTimerVisuals();
    playNotificationTone('warning');

    const id = 'positionCheckModal_' + Date.now();
    _positionCheckModalId = id;
    const response = (msg.response || 'ERROR').toString();
    const body = `
        <p><strong>No coincide la posicion esperada de la muestra.</strong></p>
        <p class="mb-1">Ciclo: <strong>${msg.cycle || '-'} / ${msg.cycles_total || '-'}</strong></p>
        <p class="mb-1">Posicion: <strong>${msg.position || '-'}</strong></p>
        <p class="mb-1">Muestra: <strong>${msg.sample_name || '—'}</strong></p>
        <p class="mb-2">Respuesta ESP32 (GETPOSITION): <strong>${response}</strong></p>
        <p class="mb-0">Coloca la muestra correctamente y elige Continuar para volver a verificar, o Cancelar para detener el experimento.</p>
    `;

    document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="${id}" tabindex="-1" data-bs-backdrop="static" data-bs-keyboard="false">
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header bg-warning text-dark">
                        <h5 class="modal-title"><i class="bi bi-exclamation-triangle-fill"></i> Verificacion de posicion</h5>
                    </div>
                    <div class="modal-body">${body}</div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-outline-danger" id="${id}_cancel">Cancelar experimento</button>
                        <button type="button" class="btn btn-success" id="${id}_continue">Continuar</button>
                    </div>
                </div>
            </div>
        </div>
    `);

    const modalEl = document.getElementById(id);
    const modal = new bootstrap.Modal(modalEl);
    const btnCancel = document.getElementById(`${id}_cancel`);
    const btnContinue = document.getElementById(`${id}_continue`);

    btnCancel.addEventListener('click', () => {
        btnCancel.disabled = true;
        btnContinue.disabled = true;
        send({ type: 'position_check_action', action: 'cancel' });
        modal.hide();
    });

    btnContinue.addEventListener('click', () => {
        btnCancel.disabled = true;
        btnContinue.disabled = true;
        send({ type: 'position_check_action', action: 'continue' });
        modal.hide();
    });

    modalEl.addEventListener('hidden.bs.modal', () => {
        if (_positionCheckModalId === id) {
            _positionCheckModalId = null;
            resumeTimerVisuals();
        }
        modalEl.remove();
    });

    modal.show();
}

function closePositionCheckModal() {
    if (!_positionCheckModalId) {
        resumeTimerVisuals();
        return;
    }
    const modalEl = document.getElementById(_positionCheckModalId);
    if (modalEl) {
        const instance = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
        instance.hide();
    } else {
        _positionCheckModalId = null;
        resumeTimerVisuals();
    }
}

// ══════════════════════════════════════════════════════════════════════════
//  EXPERIMENT PAUSE BANNER (motor / BLE errors)
// ══════════════════════════════════════════════════════════════════════════

const PAUSE_BANNER_ID = 'experiment-pause-banner';

function _esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
                          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _renderPauseBanner(msg) {
    const reasons = Array.isArray(msg.reasons) ? msg.reasons : [];
    const hasBle = reasons.some(r => r && r.type === 'ble');
    const bleConnected = !!msg.ble_connected;

    const listItems = reasons.length
        ? reasons.map(r => `<li>${_esc(r.message || r.type)}</li>`).join('')
        : '<li>Se produjo un error durante el experimento.</li>';

    const reconnectBtn = (hasBle && !bleConnected)
        ? `<button type="button" class="btn btn-warning btn-sm flex-fill" id="btn-pause-reconnect">
               <i class="bi bi-bluetooth"></i> Reconectar BLE
           </button>`
        : '';

    let banner = document.getElementById(PAUSE_BANNER_ID);
    if (!banner) {
        banner = document.createElement('div');
        banner.id = PAUSE_BANNER_ID;
        banner.className = 'experiment-pause-banner';
        document.body.appendChild(banner);
    }

    banner.innerHTML = `
        <div class="d-flex flex-wrap align-items-center gap-2">
            <i class="bi bi-exclamation-triangle-fill pause-banner-icon"></i>
            <div class="flex-fill">
                <strong class="d-block">Experimento en pausa</strong>
                <span class="pause-banner-msg">Revisa el sistema antes de continuar:</span>
                <ul class="pause-banner-list">${listItems}</ul>
            </div>
            <div class="d-flex gap-2 flex-wrap">
                ${reconnectBtn}
                <button type="button" class="btn btn-success btn-sm flex-fill" id="btn-pause-continue">
                    <i class="bi bi-play-fill"></i> Continuar
                </button>
                <button type="button" class="btn btn-outline-danger btn-sm flex-fill" id="btn-pause-cancel">
                    <i class="bi bi-x-lg"></i> Cancelar
                </button>
            </div>
        </div>`;

    const reconnectBtnEl = document.getElementById('btn-pause-reconnect');
    if (reconnectBtnEl) {
        reconnectBtnEl.addEventListener('click', () => {
            reconnectBtnEl.disabled = true;
            reconnectBtnEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Conectando…';
            connectEsp();
        });
    }
    document.getElementById('btn-pause-continue').addEventListener('click', () => {
        send({ type: 'resume_experiment' });
    });
    document.getElementById('btn-pause-cancel').addEventListener('click', () => {
        send({ type: 'cancel_experiment' });
    });
}

function _clearPauseBanner() {
    const banner = document.getElementById(PAUSE_BANNER_ID);
    if (banner) banner.remove();
}

function handleExperimentPause(msg) {
    if (!msg) return;
    const isNew = !_experimentPaused;
    _experimentPaused = true;
    _lastPauseMsg = msg;
    if (isNew) {
        playNotificationTone('warning');
        appendLog('⚠ EXPERIMENTO EN PAUSA — revisa el sistema', 'warning');
        pauseTimerVisuals();
    }
    _renderPauseBanner(msg);
}

function handleExperimentResumed() {
    if (!_experimentPaused) return;
    _experimentPaused = false;
    _lastPauseMsg = null;
    _clearPauseBanner();
    resumeTimerVisuals();
    appendLog('▶ Experimento continuando…', 'success');
}

// ══════════════════════════════════════════════════════════════════════════════//  EXPERIMENT COMPLETE / SERIAL DISCONNECT MODALS
// ══════════════════════════════════════════════════════════════════════════

function handleExperimentComplete(msg) {
    setExperimentRunning(false);
    const cancelled = msg.cancelled || false;
    const error = msg.error || '';

    if (error) {
        playNotificationTone('error');
    } else if (!cancelled) {
        playNotificationTone('success');
    }

    appendLog(
        error ? `⚠ ERROR: ${error}`
              : cancelled ? '⚠ EXPERIMENTO CANCELADO'
                          : '✓ EXPERIMENTO COMPLETADO',
        error || cancelled ? 'error' : 'success'
    );

    showModal(
        error ? 'bg-danger text-white'
              : cancelled ? 'bg-warning text-dark'
                          : 'bg-success text-white',
        error ? '<i class="bi bi-exclamation-triangle-fill"></i> Error en Experimento'
              : cancelled ? '<i class="bi bi-exclamation-triangle-fill"></i> Experimento Cancelado'
                          : '<i class="bi bi-check-circle-fill"></i> Experimento Completado',
        error ? `<p><strong>Error del motor:</strong></p><p>${error}</p><p>El experimento se detuvo. Revisa el ESP32 y la conexión antes de reiniciar.</p>`
              : cancelled ? '<p>El experimento fue cancelado. El motor ha vuelto a la posición segura.</p>'
                          : '<p class="lead">¡Experimento finalizado correctamente!</p><p>Los CSV se han descargado a tu dispositivo.</p>'
    );

    // Browser notification when page hidden
    if ('Notification' in window && document.hidden && (!cancelled || error)) {
        const body = error ? 'Error de motor en el experimento' : 'Experimento completado';
        if (Notification.permission === 'granted') {
            new Notification('TOMV3 + Banco', { body });
        } else if (Notification.permission !== 'denied') {
            Notification.requestPermission();
        }
    }
}

function handleManualExperimentComplete(msg) {
    setManualExperimentRunning(false);

    const cancelled = !!msg.cancelled;
    const success = !!msg.success && !cancelled;

    if (success) {
        playNotificationTone('success');
    }

    appendLog(
        success ? '✓ EXPERIMENTO MANUAL COMPLETADO' : '⚠ EXPERIMENTO MANUAL FINALIZADO CON ERROR/CANCELADO',
        success ? 'success' : 'error'
    );

    if (success) {
        showModal(
            'bg-success text-white',
            '<i class="bi bi-check-circle-fill"></i> Experimento Manual Completado',
            `<p class="lead">¡Experimento manual finalizado correctamente!</p><p>Muestra: <strong>${msg.sample_name || 'Manual'}</strong></p>`
        );
    } else {
        showModal(
            'bg-warning text-dark',
            '<i class="bi bi-exclamation-triangle-fill"></i> Experimento Manual Finalizado',
            `<p>${msg.message || 'El experimento manual no finalizó correctamente.'}</p>`
        );
    }
}

function handleSerialDisconnection(msg) {
    setSerialStatus('Disconnected', false);
    playNotificationTone('error');
    appendLog(`⚠ SERIAL DESCONECTADO: ${msg.message}`, 'error');
    showModal(
        'bg-danger text-white',
        '<i class="bi bi-exclamation-triangle-fill"></i> Puerto Serial Desconectado',
        `<p><strong>Se perdió la conexión serial inesperadamente.</strong></p><p>${msg.message}</p><p>Reconecta el dispositivo y vuelve a conectar.</p>`
    );
}

function showModal(headerClass, title, body) {
    const id = 'dynamicModal_' + Date.now();
    document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="${id}" tabindex="-1" data-bs-backdrop="static">
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header ${headerClass}">
                        <h5 class="modal-title">${title}</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">${body}</div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cerrar</button>
                    </div>
                </div>
            </div>
        </div>
    `);
    const el    = document.getElementById(id);
    const modal = new bootstrap.Modal(el);
    modal.show();
    el.addEventListener('hidden.bs.modal', () => el.remove());
}

// ══════════════════════════════════════════════════════════════════════════
//  POSITION TABLE (BANCO DE ENSAYO)
// ══════════════════════════════════════════════════════════════════════════

function buildPositionTable() {
    const tbody = document.getElementById('positionsTable');
    for (let i = 0; i < _maxPositions; i++) {
        const tr = document.createElement('tr');

        // Checkbox
        const tdCb = document.createElement('td');
        tdCb.className = 'ps-3';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'form-check-input';
        cb.checked = (i === 0);
        cb.addEventListener('change', () => onPositionChecked(i, cb.checked));
        checkboxes.push(cb);
        tdCb.appendChild(cb);
        tr.appendChild(tdCb);

        // Position number
        const tdNum = document.createElement('td');
        tdNum.className = 'fw-bold';
        tdNum.textContent = String(i + 1);
        tr.appendChild(tdNum);

        // Sample name
        const tdName = document.createElement('td');
        const nameInp = document.createElement('input');
        nameInp.type = 'text';
        nameInp.className = 'form-control form-control-sm';
        nameInp.placeholder = `Muestra ${i + 1}`;
        nameInp.disabled = (i !== 0);
        nameInp.addEventListener('input', scheduleAutoSave);
        sampleNameInputs.push(nameInp);
        tdName.appendChild(nameInp);
        tr.appendChild(tdName);

        tbody.appendChild(tr);
    }
}

function onPositionChecked(index, isChecked) {
    if (applyingConfig) return;
    sampleNameInputs[index].disabled = !isChecked;
    if (isChecked) {
        for (let i = 0; i < index; i++) {
            if (!checkboxes[i].checked) {
                checkboxes[i].checked = true;
                sampleNameInputs[i].disabled = false;
            }
        }
    } else {
        for (let i = index + 1; i < _maxPositions; i++) {
            if (checkboxes[i].checked) {
                checkboxes[i].checked = false;
                sampleNameInputs[i].disabled = true;
            }
        }
    }
    scheduleAutoSave();
    updateAcqHint();
}

// ══════════════════════════════════════════════════════════════════════════
//  AUTO-SAVE CONFIG
// ══════════════════════════════════════════════════════════════════════════

function scheduleAutoSave() {
    if (applyingConfig) return;
    if (_saveTimer) clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => {
        setConfigStatus('guardando…', 'warning');
        send({
            type:   'save_config',
            config: {
                desorption_time:       parseFloat(document.getElementById('desorptionTime').value) || 60,
                pre_conditioning_time: parseFloat(document.getElementById('preCondTime').value) || 300,
                cycle_gap_time:        parseFloat(document.getElementById('cycleGapTime').value) || 0,
                cycles:                parseInt(document.getElementById('experimentCycles').value, 10) || 1,
                positions:             checkboxes.map((cb, i) => ({
                    enabled: cb.checked,
                    name:    sampleNameInputs[i].value.trim(),
                })),
                experiment_params: {
                    TMin:   parseFloat(document.getElementById('param-tmin').value)    || 200,
                    TMax:   parseFloat(document.getElementById('param-tmax').value)    || 400,
                    VMin:   parseFloat(document.getElementById('param-vmin').value)    || 0.5,
                    VMax:   parseFloat(document.getElementById('param-vmax').value)    || 1.5,
                    Steps:  parseInt(document.getElementById('param-steps').value, 10) || 21,
                    Cycles: parseInt(document.getElementById('param-cycles').value, 10) || 4,
                },
                sweep_type:     document.getElementById('sweep-type').value,
                ble_address:    (document.getElementById('ble-address')?.value || '').trim(),
            },
        });
    }, 500);
}

function normalizeMacAddress(value) {
    if (!value) return '';
    const normalized = value.replace(/-/g, ':').toUpperCase();
    return /^([0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(normalized) ? normalized : '';
}

function setConfigStatus(text, type) {
    const el = document.getElementById('configStatus');
    el.textContent = text;
    el.className   = `badge bg-${type} ms-auto`;
}

// ══════════════════════════════════════════════════════════════════════════
//  ACQ DURATION HINT
// ══════════════════════════════════════════════════════════════════════════

function updateAcqHint() {
    const steps      = parseInt(document.getElementById('param-steps').value)  || 21;
    const cycles     = parseInt(document.getElementById('param-cycles').value) || 4;
    const acq        = (steps + 1) * cycles * 2 + 30;
    const preCond    = parseFloat(document.getElementById('preCondTime').value) || 300;
    const linearMotorTime = 5.1;
    const rotationalMotorTime = 5.525;

    const desorption = parseFloat(document.getElementById('desorptionTime').value) || 60;
    const expCycles  = parseInt(document.getElementById('experimentCycles').value) || 1;
    const cycleGap   = parseFloat(document.getElementById('cycleGapTime').value) || 0;
    const nEnabled   = checkboxes.filter(cb => cb.checked).length || 1;
    const perPositionSeconds = preCond + acq + desorption + (2 * linearMotorTime);
    const perCycleRotationSeconds = (nEnabled - 1) * 2 * rotationalMotorTime;
    const cycleGapTotal = Math.max(0, (expCycles - 1)) * cycleGap;
    const totalExp = Math.ceil(expCycles * ((nEnabled * perPositionSeconds) + perCycleRotationSeconds) + cycleGapTotal);
    const totalMin   = Math.ceil(totalExp / 60);

    const perPosHint = `${acq} s (barrido)  →  total ~${Math.ceil(perPositionSeconds)} s por posición (incluye motores)`;
    const expHint = `Tiempo estimado de experimento completo: ~${totalExp} s (~${totalMin} min)  —  incluye tiempos de movimiento lineal y rotacional`;

    const el1 = document.getElementById('acqTimeFormula');
    const el2 = document.getElementById('acqDurationHint');
    const el3 = document.getElementById('preCondTimeDisplay');
    if (el1) el1.innerHTML = `${perPosHint}<br><strong>${expHint}</strong>`;
    if (el2) el2.innerHTML = `Tiempo estimado por posición: ~${Math.ceil(perPositionSeconds)} s (${Math.ceil(perPositionSeconds / 60)} min)<br><strong>${expHint}</strong>`;
    if (el3) el3.textContent = preCond;
}

// ══════════════════════════════════════════════════════════════════════════
//  UI HELPERS
// ══════════════════════════════════════════════════════════════════════════

function updateConnectionStatus(status) {
    const el = document.getElementById('connection-status');
    const map = {
        connecting:   { text: 'Conectando…',    cls: 'bg-warning',              icon: 'circle-fill' },
        connected:    { text: 'Conectado',       cls: 'bg-success',              icon: 'check-circle-fill' },
        disconnected: { text: 'Desconectado',    cls: 'bg-danger',               icon: 'x-circle-fill' },
        error:        { text: 'Error',           cls: 'bg-danger',               icon: 'exclamation-triangle-fill' },
    };
    const cfg = map[status] || map.disconnected;
    el.className = `badge ${cfg.cls}`;
    el.innerHTML = `<i class="bi bi-${cfg.icon}"></i> ${cfg.text}`;
}

function appendLog(message, type = 'info') {
    const terminal = document.getElementById('log-terminal');
    if (!terminal) return;
    const shouldStickToBottom = _logAutoScrollEnabled || isLogNearBottom(terminal);
    const entry    = document.createElement('div');
    entry.className = 'log-entry';

    const ts = document.createElement('span');
    ts.className   = 'log-timestamp';
    ts.textContent = `[${new Date().toLocaleTimeString()}]`;

    const txt = document.createElement('span');
    txt.className  = `log-message${type === 'error' ? ' log-error' : ''}${type === 'success' ? ' log-success' : ''}`;
    txt.textContent = ' ' + message;

    entry.appendChild(ts);
    entry.appendChild(txt);
    terminal.appendChild(entry);

    if (shouldStickToBottom) {
        terminal.scrollTop = terminal.scrollHeight;
        _logAutoScrollEnabled = true;
    }

    while (terminal.children.length > 1000) {
        terminal.removeChild(terminal.firstChild);
    }
}

function isLogNearBottom(terminal) {
    const scrollable = terminal.scrollHeight - terminal.clientHeight;
    const distance = scrollable - terminal.scrollTop;
    return distance <= LOG_NEAR_BOTTOM_PX;
}

function clearLogTerminal() {
    document.getElementById('log-terminal').innerHTML = '';
    _logAutoScrollEnabled = true;
    appendLog('Log limpiado');
}

function showAlert(message, type = 'info') {
    if (type === 'danger') {
        playNotificationTone('error');
    }

    const div = document.createElement('div');
    div.className = `alert alert-${type} alert-dismissible fade show position-fixed top-0 start-50 translate-middle-x mt-3`;
    div.style.zIndex = '9999';
    div.style.minWidth = '320px';
    div.innerHTML = `${message}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`;
    document.body.appendChild(div);
    setTimeout(() => div.remove(), 5000);
}

function setVal(id, value) {
    const el = document.getElementById(id);
    if (el && value !== undefined && value !== null) el.value = value;
}

// ── Start ──────────────────────────────────────────────────────────────────
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

