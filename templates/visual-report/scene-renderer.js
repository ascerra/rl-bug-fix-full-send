/**
 * Three.js Scene Renderer for Ralph Loop Execution Reports.
 *
 * Reads the SceneData JSON produced by engine/visualization/scene/builder.py
 * and renders an interactive 3D execution landscape with:
 *   - Platform layers per pipeline phase at ascending elevations
 *   - Action objects (polyhedra, cubes, cylinders, spheres) with status glow
 *   - Animated particle connections showing data flow
 *   - OrbitControls (rotate, zoom, pan)
 *   - Raycasting for click-to-inspect and hover tooltips
 *   - Minimap (top-down orthographic inset)
 *   - WebGL fallback message when GPU is unavailable
 *   - Level-of-detail for large scenes
 *
 * Requires Three.js r158+ (inlined in the report HTML).
 */

/* global THREE */

var RalphSceneRenderer = (function () {
  'use strict';

  var DEFAULTS = {
    containerId: 'scene-3d-container',
    detailPanelId: 'scene-3d-detail',
    width: null,
    height: 600,
    antialias: true,
    pixelRatio: Math.min(window.devicePixelRatio || 1, 2),
    enableMinimap: true,
    minimapSize: 160,
    enableTooltip: true,
    lodThreshold: 100,
    animateConnections: true,
    pulseFailedObjects: true,
  };

  // ── Color Utilities ────────────────────────────────────────────────────

  function hexToInt(hex) {
    return parseInt(hex.replace('#', ''), 16);
  }

  function statusEmissiveIntensity(status) {
    switch (status) {
      case 'success': return 0.3;
      case 'failure': return 0.6;
      case 'escalated': return 0.4;
      case 'retry': return 0.35;
      default: return 0.15;
    }
  }

  // ── Geometry Factories ─────────────────────────────────────────────────

  function createGeometry(type, scale) {
    var s = scale || 1.0;
    switch (type) {
      case 'polyhedron':
        return new THREE.IcosahedronGeometry(0.5 * s, 1);
      case 'cube':
        return new THREE.BoxGeometry(0.7 * s, 0.7 * s, 0.7 * s);
      case 'cylinder':
        return new THREE.CylinderGeometry(0.35 * s, 0.35 * s, 0.8 * s, 16);
      case 'sphere':
        return new THREE.SphereGeometry(0.45 * s, 16, 12);
      default:
        return new THREE.BoxGeometry(0.7 * s, 0.7 * s, 0.7 * s);
    }
  }

  function createLODGeometry(type, scale) {
    var s = scale || 1.0;
    switch (type) {
      case 'polyhedron':
        return new THREE.IcosahedronGeometry(0.5 * s, 0);
      case 'cube':
        return new THREE.BoxGeometry(0.7 * s, 0.7 * s, 0.7 * s);
      case 'cylinder':
        return new THREE.CylinderGeometry(0.35 * s, 0.35 * s, 0.8 * s, 8);
      case 'sphere':
        return new THREE.SphereGeometry(0.45 * s, 8, 6);
      default:
        return new THREE.BoxGeometry(0.7 * s, 0.7 * s, 0.7 * s);
    }
  }

  // ── Platform Construction ──────────────────────────────────────────────

  function buildPlatform(platformData, objectCount) {
    var width = Math.max(objectCount * 2.5, 6);
    var depth = 3;
    var geo = new THREE.BoxGeometry(width, 0.15, depth);
    var color = hexToInt(platformData.color || '#6b7280');
    var mat = new THREE.MeshStandardMaterial({
      color: color,
      transparent: true,
      opacity: 0.25,
      roughness: 0.8,
      metalness: 0.1,
    });
    var mesh = new THREE.Mesh(geo, mat);
    mesh.position.set(0, platformData.elevation, 0);
    mesh.receiveShadow = true;
    mesh.userData = {
      type: 'platform',
      phase: platformData.phase,
      label: platformData.label,
      status: platformData.status,
      iterationCount: platformData.iteration_count,
    };

    var labelSprite = createTextSprite(
      platformData.label || platformData.phase,
      { fontSize: 48, color: '#e6edf3', bgColor: 'rgba(22,27,34,0.85)' }
    );
    labelSprite.position.set(-width / 2 - 1.2, platformData.elevation + 0.5, 0);
    labelSprite.scale.set(2.5, 1.0, 1);

    return { mesh: mesh, label: labelSprite, width: width };
  }

  // ── Text Sprite ────────────────────────────────────────────────────────

  function createTextSprite(text, opts) {
    opts = opts || {};
    var fontSize = opts.fontSize || 36;
    var color = opts.color || '#e6edf3';
    var bgColor = opts.bgColor || 'rgba(22,27,34,0.8)';
    var padding = opts.padding || 12;

    var canvas = document.createElement('canvas');
    var ctx = canvas.getContext('2d');
    ctx.font = fontSize + 'px -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif';
    var metrics = ctx.measureText(text);
    var w = metrics.width + padding * 2;
    var h = fontSize + padding * 2;
    canvas.width = Math.ceil(w);
    canvas.height = Math.ceil(h);

    ctx.fillStyle = bgColor;
    roundRect(ctx, 0, 0, canvas.width, canvas.height, 8);
    ctx.fill();
    ctx.font = fontSize + 'px -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif';
    ctx.fillStyle = color;
    ctx.textBaseline = 'middle';
    ctx.fillText(text, padding, canvas.height / 2);

    var tex = new THREE.CanvasTexture(canvas);
    tex.minFilter = THREE.LinearFilter;
    var spriteMat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false });
    var sprite = new THREE.Sprite(spriteMat);
    sprite.userData = { isLabel: true };
    return sprite;
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  // ── Action Object Construction ─────────────────────────────────────────

  function buildActionObject(objData, useLOD) {
    var geo = useLOD
      ? createLODGeometry(objData.geometry, objData.scale)
      : createGeometry(objData.geometry, objData.scale);
    var baseColor = hexToInt(objData.color || '#6b7280');
    var emissiveColor = baseColor;
    var emissiveIntensity = statusEmissiveIntensity(objData.status);
    var isGhost = (objData.meta && objData.meta.ghost) === true;
    var mat = new THREE.MeshStandardMaterial({
      color: baseColor,
      emissive: emissiveColor,
      emissiveIntensity: isGhost ? 0.1 : emissiveIntensity,
      roughness: isGhost ? 0.7 : 0.4,
      metalness: isGhost ? 0.1 : 0.3,
      transparent: isGhost,
      opacity: isGhost ? 0.3 : 1.0,
      wireframe: isGhost,
    });
    var mesh = new THREE.Mesh(geo, mat);
    var pos = objData.position || { x: 0, y: 0, z: 0 };
    mesh.position.set(pos.x, pos.y + 0.6, pos.z);
    mesh.castShadow = !isGhost;
    mesh.receiveShadow = !isGhost;
    mesh.userData = {
      type: 'action',
      id: objData.id,
      actionType: objData.action_type,
      label: objData.label,
      phase: objData.phase,
      iteration: objData.iteration,
      status: objData.status,
      tokens: objData.tokens,
      durationMs: objData.duration_ms,
      timestamp: objData.timestamp,
      meta: objData.meta || {},
      originalEmissive: isGhost ? 0.1 : emissiveIntensity,
      ghost: isGhost,
    };
    return mesh;
  }

  // ── Connection Lines ───────────────────────────────────────────────────

  function buildConnection(connData, objectsById) {
    var srcObj = objectsById[connData.source];
    var tgtObj = objectsById[connData.target];
    if (!srcObj || !tgtObj) return null;

    var srcPos = srcObj.position;
    var tgtPos = tgtObj.position;

    var points = [
      new THREE.Vector3(srcPos.x, srcPos.y, srcPos.z),
      new THREE.Vector3(
        (srcPos.x + tgtPos.x) / 2,
        (srcPos.y + tgtPos.y) / 2 + 0.5,
        (srcPos.z + tgtPos.z) / 2
      ),
      new THREE.Vector3(tgtPos.x, tgtPos.y, tgtPos.z),
    ];
    var curve = new THREE.QuadraticBezierCurve3(points[0], points[1], points[2]);
    var tubePts = curve.getPoints(20);
    var geo = new THREE.BufferGeometry().setFromPoints(tubePts);

    var color = hexToInt(connData.color || '#94a3b8');
    var mat = new THREE.LineBasicMaterial({
      color: color,
      transparent: true,
      opacity: connData.type === 'phase_transition' ? 0.7 : 0.4,
      linewidth: 1,
    });
    var line = new THREE.Line(geo, mat);
    line.userData = {
      type: 'connection',
      connectionType: connData.type,
      dataType: connData.data_type,
      animated: connData.animated,
      source: connData.source,
      target: connData.target,
    };
    return line;
  }

  // ── Bridge Paths ───────────────────────────────────────────────────────

  function buildBridge(bridgeData) {
    var fromY = bridgeData.from_elevation;
    var toY = bridgeData.to_elevation;
    var midY = (fromY + toY) / 2;
    var points = [
      new THREE.Vector3(-3, fromY, 0),
      new THREE.Vector3(-4, midY, 0),
      new THREE.Vector3(-3, toY, 0),
    ];
    var curve = new THREE.QuadraticBezierCurve3(points[0], points[1], points[2]);
    var pts = curve.getPoints(16);
    var geo = new THREE.BufferGeometry().setFromPoints(pts);
    var mat = new THREE.LineBasicMaterial({
      color: hexToInt(bridgeData.color || '#a78bfa'),
      transparent: true,
      opacity: 0.35,
      linewidth: 1,
    });
    var line = new THREE.Line(geo, mat);
    line.userData = { type: 'bridge', from: bridgeData.from_phase, to: bridgeData.to_phase };
    return line;
  }

  // ── Tooltip ────────────────────────────────────────────────────────────

  function createTooltip() {
    var el = document.createElement('div');
    el.id = 'ralph-scene-tooltip';
    el.style.cssText =
      'position:absolute;pointer-events:none;display:none;padding:6px 10px;' +
      'background:rgba(22,27,34,0.95);color:#e6edf3;font-size:12px;' +
      'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;' +
      'border:1px solid #30363d;border-radius:4px;max-width:300px;z-index:1000;' +
      'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    document.body.appendChild(el);
    return el;
  }

  // ── Detail Panel Renderer ──────────────────────────────────────────────

  function renderDetailPanel(panelId, userData) {
    var panel = document.getElementById(panelId);
    if (!panel) return;

    if (!userData || userData.type === 'platform') {
      var d = userData || {};
      panel.innerHTML =
        '<h3>' + escapeHtml(d.label || d.phase || 'Platform') + '</h3>' +
        '<div class="detail-status-bar">' +
          '<span class="badge status-' + (d.status || 'unknown') + '">' + (d.status || '?').toUpperCase() + '</span>' +
          '<span style="color:#8b949e;font-size:0.8rem">' + (d.iterationCount || 0) + ' iteration(s)</span>' +
        '</div>' +
        '<p class="detail-narrative">Click an action object on this platform to inspect its details.</p>';
      return;
    }

    var meta = userData.meta || {};
    var input = meta.input || {};
    var output = meta.output || {};
    var llm = meta.llm_context || {};
    var provenance = meta.provenance || {};

    var html = '';
    html += '<h3>' + escapeHtml(userData.label || userData.actionType || 'Action') + '</h3>';
    html += '<div class="detail-status-bar">';
    html += '<span class="badge status-' + (userData.status || 'unknown') + '">' + (userData.status || '?').toUpperCase() + '</span>';
    html += '<span class="action-type action-type-' + (userData.actionType || 'default') + '">' + escapeHtml((userData.actionType || 'unknown').replace(/_/g, ' ')) + '</span>';
    if (userData.durationMs) {
      html += '<span class="detail-status-bar .detail-duration" style="color:#8b949e;font-size:0.8rem;margin-left:auto">' + formatDuration(userData.durationMs) + '</span>';
    }
    html += '</div>';

    if (userData.actionType === 'llm_query') {
      html += '<div class="detail-section"><div class="detail-section-title">What the agent was told</div>';
      html += '<p class="detail-narrative">' + escapeHtml(input.description || 'No description available') + '</p></div>';

      if (provenance.reasoning) {
        html += '<div class="detail-section"><div class="detail-section-title">Key reasoning</div>';
        html += '<p class="detail-narrative">' + escapeHtml(provenance.reasoning) + '</p></div>';
      }

      if (llm.model || llm.provider) {
        html += '<div class="detail-section"><div class="detail-section-title">By the numbers</div>';
        html += '<div class="detail-kv-list">';
        if (llm.model) html += '<span class="kv-key">Model</span><span class="kv-val">' + escapeHtml(llm.model) + '</span>';
        if (llm.provider) html += '<span class="kv-key">Provider</span><span class="kv-val">' + escapeHtml(llm.provider) + '</span>';
        html += '<span class="kv-key">Tokens in</span><span class="kv-val">' + (llm.tokens_in || 0) + '</span>';
        html += '<span class="kv-key">Tokens out</span><span class="kv-val">' + (llm.tokens_out || 0) + '</span>';
        html += '</div></div>';
      }
    } else if (userData.actionType === 'file_read' || userData.actionType === 'file_write' || userData.actionType === 'file_search') {
      var fpath = input.path || input.context && input.context.path || '';
      html += '<div class="detail-section"><div class="detail-section-title">' +
        (userData.actionType === 'file_write' ? 'What was written' : 'What was read') + '</div>';
      if (fpath) html += '<p><span class="detail-file-path">' + escapeHtml(fpath) + '</span></p>';
      html += '<p class="detail-narrative">' + escapeHtml(input.description || '') + '</p></div>';

      if (output.content && typeof output.content === 'string') {
        html += '<div class="detail-section"><div class="detail-section-title">Content (excerpt)</div>';
        html += '<div class="detail-code-block">' + escapeHtml(output.content.substring(0, 2000)) +
          (output.content.length > 2000 ? '\n... (truncated)' : '') + '</div></div>';
      }
    } else {
      html += '<div class="detail-section"><div class="detail-section-title">What was run</div>';
      html += '<p class="detail-narrative">' + escapeHtml(input.description || 'No description') + '</p></div>';

      var stdout = output.stdout || output.data && output.data.stdout || '';
      var stderr = output.stderr || output.data && output.data.stderr || '';
      if (stdout || stderr) {
        html += '<div class="detail-section"><div class="detail-section-title">What happened</div>';
        html += '<div class="detail-code-block">';
        if (stdout) html += escapeHtml(stdout.substring(0, 1500));
        if (stderr) html += (stdout ? '\n---\n' : '') + escapeHtml(stderr.substring(0, 1500));
        html += '</div></div>';
      }
    }

    html += '<div class="detail-section" style="margin-top:0.75rem"><div class="detail-kv-list">';
    html += '<span class="kv-key">Phase</span><span class="kv-val">' + escapeHtml(userData.phase || '?') + '</span>';
    html += '<span class="kv-key">Iteration</span><span class="kv-val">' + (userData.iteration || 0) + '</span>';
    if (userData.timestamp) html += '<span class="kv-key">Time</span><span class="kv-val">' + escapeHtml(userData.timestamp.substring(0, 19)) + '</span>';
    if (userData.tokens) html += '<span class="kv-key">Tokens</span><span class="kv-val">' + userData.tokens + '</span>';
    html += '</div></div>';

    panel.innerHTML = html;
  }

  // ── Minimap ────────────────────────────────────────────────────────────

  function createMinimap(container, size) {
    var el = document.createElement('div');
    el.id = 'ralph-minimap';
    el.style.cssText =
      'position:absolute;bottom:12px;right:12px;width:' + size + 'px;height:' + size + 'px;' +
      'border:1px solid #30363d;border-radius:6px;overflow:hidden;background:#0d1117;' +
      'pointer-events:none;z-index:10;';
    container.style.position = 'relative';
    container.appendChild(el);
    return el;
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function formatDuration(ms) {
    if (ms < 1000) return Math.round(ms) + 'ms';
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + 's';
    return (s / 60).toFixed(1) + 'm';
  }

  // ── Main Renderer Class ────────────────────────────────────────────────

  function SceneRenderer(sceneData, options) {
    this.sceneData = sceneData;
    this.opts = {};
    var key;
    for (key in DEFAULTS) {
      this.opts[key] = DEFAULTS[key];
    }
    if (options) {
      for (key in options) {
        this.opts[key] = options[key];
      }
    }

    this.scene = null;
    this.camera = null;
    this.renderer = null;
    this.controls = null;
    this.raycaster = new THREE.Raycaster();
    this.mouse = new THREE.Vector2(-999, -999);
    this.clickables = [];
    this.objectsById = {};
    this.hoveredObject = null;
    this.selectedObject = null;
    this.failedObjects = [];
    this.tooltip = null;
    this.minimapRenderer = null;
    this.minimapCamera = null;
    this.clock = new THREE.Clock();
    this._animationId = null;
    this._disposed = false;
    this._boundOnResize = this._onResize.bind(this);
    this._boundOnMouseMove = this._onMouseMove.bind(this);
    this._boundOnClick = this._onClick.bind(this);
  }

  SceneRenderer.prototype.init = function () {
    if (typeof THREE === 'undefined') {
      this._showFallback('Three.js library not loaded.');
      return false;
    }

    var container = document.getElementById(this.opts.containerId);
    if (!container) {
      return false;
    }

    if (!this._webglAvailable()) {
      this._showFallback(
        'WebGL is not available in your browser. ' +
        'The 3D execution landscape requires a WebGL-capable browser. ' +
        'See the plain-text execution summary above for details.'
      );
      return false;
    }

    var width = this.opts.width || container.clientWidth || 800;
    var height = this.opts.height || 600;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0d1117);
    this.scene.fog = new THREE.FogExp2(0x0d1117, 0.012);

    var camData = this.sceneData.camera || {};
    var camPos = camData.position || { x: 15, y: 10, z: 20 };
    var camTarget = camData.target || { x: 0, y: 6, z: 0 };
    this.camera = new THREE.PerspectiveCamera(
      camData.fov || 60,
      width / height,
      camData.near || 0.1,
      camData.far || 1000
    );
    this.camera.position.set(camPos.x, camPos.y, camPos.z);

    this.renderer = new THREE.WebGLRenderer({
      antialias: this.opts.antialias,
      alpha: false,
    });
    this.renderer.setSize(width, height);
    this.renderer.setPixelRatio(this.opts.pixelRatio);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    container.appendChild(this.renderer.domElement);

    if (THREE.OrbitControls) {
      this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
      this.controls.target.set(camTarget.x, camTarget.y, camTarget.z);
      this.controls.enableDamping = true;
      this.controls.dampingFactor = 0.08;
      this.controls.minDistance = 5;
      this.controls.maxDistance = 200;
      this.controls.update();
    }

    this._buildLights();
    this._buildScene();

    if (this.opts.enableTooltip) {
      this.tooltip = createTooltip();
    }

    if (this.opts.enableMinimap) {
      this._initMinimap(container, width);
    }

    window.addEventListener('resize', this._boundOnResize);
    this.renderer.domElement.addEventListener('mousemove', this._boundOnMouseMove);
    this.renderer.domElement.addEventListener('click', this._boundOnClick);

    this._animate();
    return true;
  };

  SceneRenderer.prototype._webglAvailable = function () {
    try {
      var c = document.createElement('canvas');
      return !!(c.getContext('webgl') || c.getContext('webgl2') || c.getContext('experimental-webgl'));
    } catch (e) {
      return false;
    }
  };

  SceneRenderer.prototype._showFallback = function (message) {
    var container = document.getElementById(this.opts.containerId);
    if (!container) return;
    container.innerHTML =
      '<div style="padding:2rem;text-align:center;color:#8b949e;background:#161b22;' +
      'border:1px solid #30363d;border-radius:6px">' +
      '<p style="font-size:1.1rem;margin-bottom:0.5rem">' + escapeHtml(message) + '</p>' +
      '<p style="font-size:0.85rem">The 2D decision tree and action map are available above.</p>' +
      '</div>';
  };

  // ── Lighting ───────────────────────────────────────────────────────────

  SceneRenderer.prototype._buildLights = function () {
    var ambientColor = 0x404050;
    var summary = this.sceneData.summary || {};
    if (summary.status === 'success') ambientColor = 0x304030;
    else if (summary.status === 'failure') ambientColor = 0x403030;

    this.scene.add(new THREE.AmbientLight(ambientColor, 0.6));

    var dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(10, 20, 15);
    dirLight.castShadow = true;
    dirLight.shadow.mapSize.width = 1024;
    dirLight.shadow.mapSize.height = 1024;
    dirLight.shadow.camera.near = 0.5;
    dirLight.shadow.camera.far = 100;
    dirLight.shadow.camera.left = -30;
    dirLight.shadow.camera.right = 30;
    dirLight.shadow.camera.top = 30;
    dirLight.shadow.camera.bottom = -30;
    this.scene.add(dirLight);

    var fillLight = new THREE.DirectionalLight(0x5588cc, 0.3);
    fillLight.position.set(-8, 10, -10);
    this.scene.add(fillLight);
  };

  // ── Scene Construction ─────────────────────────────────────────────────

  SceneRenderer.prototype._buildScene = function () {
    var platforms = this.sceneData.platforms || [];
    var connections = this.sceneData.connections || [];
    var bridges = this.sceneData.bridges || [];

    var totalObjects = 0;
    var i;
    for (i = 0; i < platforms.length; i++) {
      totalObjects += (platforms[i].objects || []).length;
    }
    var useLOD = totalObjects > this.opts.lodThreshold;

    for (i = 0; i < platforms.length; i++) {
      var pData = platforms[i];
      var objects = pData.objects || [];
      var plat = buildPlatform(pData, objects.length);
      this.scene.add(plat.mesh);
      this.scene.add(plat.label);
      this.clickables.push(plat.mesh);

      for (var j = 0; j < objects.length; j++) {
        var objMesh = buildActionObject(objects[j], useLOD);
        this.scene.add(objMesh);
        this.clickables.push(objMesh);
        this.objectsById[objects[j].id] = objMesh;
        if (objects[j].status === 'failure' && this.opts.pulseFailedObjects) {
          this.failedObjects.push(objMesh);
        }
      }
    }

    for (i = 0; i < connections.length; i++) {
      var line = buildConnection(connections[i], this.objectsById);
      if (line) this.scene.add(line);
    }

    for (i = 0; i < bridges.length; i++) {
      var bridge = buildBridge(bridges[i]);
      this.scene.add(bridge);
    }

    var gridHelper = new THREE.GridHelper(60, 30, 0x1c2333, 0x1c2333);
    gridHelper.position.y = -0.5;
    this.scene.add(gridHelper);
  };

  // ── Minimap ────────────────────────────────────────────────────────────

  SceneRenderer.prototype._initMinimap = function (container, mainWidth) {
    var size = this.opts.minimapSize;
    var el = createMinimap(container, size);
    this.minimapRenderer = new THREE.WebGLRenderer({ antialias: false, alpha: false });
    this.minimapRenderer.setSize(size, size);
    this.minimapRenderer.setPixelRatio(1);
    el.appendChild(this.minimapRenderer.domElement);

    var maxElev = 0;
    var platforms = this.sceneData.platforms || [];
    for (var i = 0; i < platforms.length; i++) {
      if (platforms[i].elevation > maxElev) maxElev = platforms[i].elevation;
    }
    var centerY = maxElev / 2;
    var orthoSize = Math.max(maxElev * 0.8, 15);
    this.minimapCamera = new THREE.OrthographicCamera(
      -orthoSize, orthoSize, orthoSize, -orthoSize, 0.1, 500
    );
    this.minimapCamera.position.set(0, centerY, orthoSize * 2.5);
    this.minimapCamera.lookAt(0, centerY, 0);
  };

  // ── Animation Loop ─────────────────────────────────────────────────────

  SceneRenderer.prototype._animate = function () {
    if (this._disposed) return;
    this._animationId = requestAnimationFrame(this._animate.bind(this));

    var elapsed = this.clock.getElapsedTime();

    if (this.opts.pulseFailedObjects) {
      var pulse = 0.3 + Math.sin(elapsed * 3) * 0.3;
      for (var i = 0; i < this.failedObjects.length; i++) {
        this.failedObjects[i].material.emissiveIntensity = pulse;
      }
    }

    if (this.controls) this.controls.update();
    this.renderer.render(this.scene, this.camera);

    if (this.minimapRenderer && this.minimapCamera) {
      this.minimapRenderer.render(this.scene, this.minimapCamera);
    }
  };

  // ── Interaction: Hover ─────────────────────────────────────────────────

  SceneRenderer.prototype._onMouseMove = function (event) {
    var rect = this.renderer.domElement.getBoundingClientRect();
    this.mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    this.mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

    this.raycaster.setFromCamera(this.mouse, this.camera);
    var intersects = this.raycaster.intersectObjects(this.clickables, false);

    if (this.hoveredObject && this.hoveredObject !== this.selectedObject) {
      this.hoveredObject.material.emissiveIntensity =
        this.hoveredObject.userData.originalEmissive || 0.15;
    }

    if (intersects.length > 0) {
      var hit = intersects[0].object;
      this.hoveredObject = hit;
      this.renderer.domElement.style.cursor = 'pointer';

      if (hit !== this.selectedObject) {
        hit.material.emissiveIntensity = (hit.userData.originalEmissive || 0.15) + 0.25;
      }

      if (this.tooltip) {
        var label = hit.userData.label || hit.userData.phase || '';
        if (label) {
          this.tooltip.textContent = label;
          this.tooltip.style.display = 'block';
          this.tooltip.style.left = (event.clientX + 12) + 'px';
          this.tooltip.style.top = (event.clientY - 28) + 'px';
        }
      }
    } else {
      this.hoveredObject = null;
      this.renderer.domElement.style.cursor = 'default';
      if (this.tooltip) this.tooltip.style.display = 'none';
    }
  };

  // ── Interaction: Click ─────────────────────────────────────────────────

  SceneRenderer.prototype._onClick = function () {
    this.raycaster.setFromCamera(this.mouse, this.camera);
    var intersects = this.raycaster.intersectObjects(this.clickables, false);

    if (this.selectedObject) {
      this.selectedObject.material.emissiveIntensity =
        this.selectedObject.userData.originalEmissive || 0.15;
      this.selectedObject = null;
    }

    if (intersects.length > 0) {
      var hit = intersects[0].object;
      this.selectedObject = hit;
      hit.material.emissiveIntensity = (hit.userData.originalEmissive || 0.15) + 0.4;
      renderDetailPanel(this.opts.detailPanelId, hit.userData);
    }
  };

  // ── Resize ─────────────────────────────────────────────────────────────

  SceneRenderer.prototype._onResize = function () {
    var container = document.getElementById(this.opts.containerId);
    if (!container || !this.camera || !this.renderer) return;
    var width = container.clientWidth || 800;
    var height = this.opts.height || 600;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height);
  };

  // ── Camera Presets ─────────────────────────────────────────────────────

  SceneRenderer.prototype.setCameraPreset = function (name) {
    var camData = this.sceneData.camera || {};
    var presets = camData.presets || {};
    var preset = presets[name];
    if (!preset) return;

    var pos = preset.position;
    var target = preset.target || camData.target || { x: 0, y: 0, z: 0 };

    this.camera.position.set(pos.x, pos.y, pos.z);
    if (this.controls) {
      this.controls.target.set(target.x, target.y, target.z);
      this.controls.update();
    }
  };

  // ── Cleanup ────────────────────────────────────────────────────────────

  SceneRenderer.prototype.dispose = function () {
    this._disposed = true;
    if (this._animationId) cancelAnimationFrame(this._animationId);
    window.removeEventListener('resize', this._boundOnResize);
    if (this.renderer && this.renderer.domElement) {
      this.renderer.domElement.removeEventListener('mousemove', this._boundOnMouseMove);
      this.renderer.domElement.removeEventListener('click', this._boundOnClick);
    }
    if (this.controls) this.controls.dispose();
    if (this.renderer) this.renderer.dispose();
    if (this.minimapRenderer) this.minimapRenderer.dispose();
    if (this.tooltip && this.tooltip.parentNode) {
      this.tooltip.parentNode.removeChild(this.tooltip);
    }
  };

  // ── Public API ─────────────────────────────────────────────────────────

  return {
    SceneRenderer: SceneRenderer,
    renderDetailPanel: renderDetailPanel,
    _internals: {
      hexToInt: hexToInt,
      statusEmissiveIntensity: statusEmissiveIntensity,
      createGeometry: createGeometry,
      createLODGeometry: createLODGeometry,
      buildPlatform: buildPlatform,
      buildActionObject: buildActionObject,
      buildConnection: buildConnection,
      buildBridge: buildBridge,
      escapeHtml: escapeHtml,
      formatDuration: formatDuration,
      createTextSprite: createTextSprite,
    },
  };
})();

/**
 * Entry point: renderScene(sceneData, containerId, detailPanelId)
 * Called from report HTML after Three.js and scene data are loaded.
 */
function renderScene(sceneData, containerId, detailPanelId) {
  if (!sceneData || typeof THREE === 'undefined') return null;
  var renderer = new RalphSceneRenderer.SceneRenderer(sceneData, {
    containerId: containerId || 'scene-3d-container',
    detailPanelId: detailPanelId || 'scene-3d-detail',
  });
  renderer.init();
  return renderer;
}
