import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

// Published local copies of the credited models; populated when the assets are selected.
export const HOW_MODEL_ASSETS = {
  laptopUrl: './assets/models/laptop/macbook-open.glb',
  piUrl: './assets/models/devices/raspberry-pi.glb',
  boardUrl: './assets/models/chess/chess-set.gltf', serverUrl: './assets/models/devices/relay-server.glb',
  laptopRotation: [0, 0, 0], piRotation: [0, -Math.PI / 2, 0],
  boardRotation: [0, Math.PI, 0], serverRotation: [0, 0, 0],
};

// A separate, lazy scene. Every animation below is driven by scroll, never by a timer.
export function mountHowItWorks(section = document.querySelector('#how-it-works'), modelOptions = HOW_MODEL_ASSETS) {
  if (!section || section.dataset.howMounted === 'true') return () => {};
  const stage = section.querySelector('.how-stage');
  const canvas = section.querySelector('#how-scene');
  if (!stage || !canvas) return () => {};
  section.dataset.howMounted = 'true';
  const steps = [...section.querySelectorAll('.how-step')];
  const overview = section.querySelector('.how-overview');
  const labels = [...section.querySelectorAll('.how-node-label')];
  const motionQuery = matchMedia('(prefers-reduced-motion: reduce)');
  const svgNamespace = 'http://www.w3.org/2000/svg';
  const callouts = document.createElementNS(svgNamespace, 'svg');
  callouts.classList.add('how-callouts'); callouts.setAttribute('aria-hidden', 'true');
  callouts.setAttribute('preserveAspectRatio', 'none'); callouts.style.opacity = '0';
  const calloutParts = Array.from({ length: 2 }, () => {
    const group = document.createElementNS(svgNamespace, 'g');
    const path = document.createElementNS(svgNamespace, 'path');
    path.setAttribute('fill', 'none'); path.setAttribute('stroke', '#ffffff');
    path.setAttribute('stroke-width', '2'); path.setAttribute('vector-effect', 'non-scaling-stroke');
    const circle = document.createElementNS(svgNamespace, 'circle');
    circle.setAttribute('r', '3.5'); circle.setAttribute('fill', '#f6f6f3');
    const label = document.createElementNS(svgNamespace, 'text');
    label.setAttribute('text-anchor', 'end'); label.setAttribute('fill', '#d4cadf');
    label.setAttribute('font-size', '12'); label.setAttribute('font-family', 'Avenir Next, Avenir, system-ui, sans-serif');
    group.append(path, circle, label); callouts.append(group);
    return { group, path, circle, label };
  });
  stage.append(callouts);
  const calloutNames = [
    ['Browser interface', 'Your move'], ['Access check', 'Protected relay'],
    ['Stockfish processor', 'Internet connection'], ['Mechanical movement (planned)', 'Actuator connection (planned)'],
    ['Confirmed position', 'Ready for your next move'],
  ];
  const featureAnchors = [];
  let calloutStation = -1, lastMetricsTime = -Infinity;
  const clamp = THREE.MathUtils.clamp;
  const mix = THREE.MathUtils.lerp;
  const smooth = (a, b, value) => { const t = clamp((value - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
  const phases = ['website', 'relay', 'raspberry-pi', 'mechanical-board', 'reply'];
  const modelCache = new Map();
  const materials = new Set(), geometries = new Set(), textures = new Set();
  let renderer, scene, camera, routeMeshes, packet, routes, movingPawn, environmentTarget;
  let initialized = false, ready = false, failed = false, disposed = false, pageActive = true;
  let width = 1, height = 1, start = 0, range = 1, viewportHeight = 1;
  let target = 0, progress = 0, previousTime = 0, lastProgress = -1, dirty = true;
  let raf = 0, frameCount = 0, activeCopy = -2, inView = false;
  const point = new THREE.Vector3(), position = new THREE.Vector3(), targetPoint = new THREE.Vector3();
  const matrix = new THREE.Matrix4(), quaternion = new THREE.Quaternion(), scale = new THREE.Vector3();

  function exposeStatic(reason) {
    cancelFrame(reason);
    section.classList.toggle('is-reduced', reason === 'reduced');
    section.classList.toggle('is-fallback', reason === 'fallback' || reason === 'reduced');
    steps.forEach(element => { element.style.opacity = '1'; element.inert = false; element.setAttribute('aria-hidden', 'false'); });
    if (overview) { overview.style.opacity = '1'; overview.inert = false; overview.setAttribute('aria-hidden', 'false'); }
    labels.forEach(element => { element.style.opacity = '0'; element.setAttribute('aria-hidden', 'true'); });
    callouts.style.display = 'none';
  }

  function material(values, basic = false) {
    const result = basic ? new THREE.MeshBasicMaterial(values) : new THREE.MeshStandardMaterial(values);
    materials.add(result); return result;
  }
  function geometry(result) { geometries.add(result); return result; }
  async function loadModel(url) {
    if (!url) throw new Error('A credited device model has not been configured');
    if (!modelCache.has(url)) {
      const loader = new GLTFLoader();
      modelCache.set(url, loader.loadAsync(url).then(gltf => {
        gltf.scene.traverse(object => {
          if (!object.isMesh) return;
          geometries.add(object.geometry);
          const surfaces = Array.isArray(object.material) ? object.material : [object.material];
          surfaces.forEach(surface => {
            materials.add(surface);
            for (const value of Object.values(surface)) if (value?.isTexture) textures.add(value);
          });
        });
        return gltf.scene;
      }));
    }
    return modelCache.get(url);
  }

  function prepareModel(template, rotation, fit, featureRatios) {
    const content = template.clone(true), orient = new THREE.Group(), device = new THREE.Group();
    orient.add(content); orient.rotation.set(...rotation); orient.updateMatrixWorld(true);
    const bounds = new THREE.Box3().setFromObject(orient), size = bounds.getSize(new THREE.Vector3());
    if (!Number.isFinite(size.length()) || size.length() < .000001) throw new Error('The device model is empty');
    const factor = Math.min(...fit.map((limit, axis) => limit / Math.max(size.getComponent(axis), .000001)));
    const center = bounds.getCenter(new THREE.Vector3());
    orient.scale.setScalar(factor); orient.position.set(-center.x * factor, -bounds.min.y * factor, -center.z * factor);
    device.add(orient); device.updateMatrixWorld(true);
    const fitted = new THREE.Box3().setFromObject(device), span = fitted.getSize(new THREE.Vector3());
    device.userData.features = featureRatios.map(([x, y, z]) => ({ object: device,
      point: new THREE.Vector3(fitted.min.x + span.x * x, fitted.min.y + span.y * y, fitted.min.z + span.z * z) }));
    return device;
  }

  function geometrySlice(source, start, count) {
    const part = new THREE.BufferGeometry();
    for (const [name, attribute] of Object.entries(source.attributes)) {
      // Quantized GLTF attributes must be expanded before baking world positions;
      // writing a 32-unit translation back into normalized Int16 would clip it.
      const values = new Float32Array(count * attribute.itemSize);
      for (let i = 0; i < count; i++) for (let j = 0; j < attribute.itemSize; j++) values[i * attribute.itemSize + j] = attribute.getComponent(i + start, j);
      part.setAttribute(name, new THREE.BufferAttribute(values, attribute.itemSize));
    }
    return part;
  }

  function applyLaptopScreen(device, confirmed) {
    const display = device.getObjectByName('Object_7');
    if (!display?.isMesh) throw new Error('The downloaded laptop screen could not be located');
    // The downloaded display is almost coplanar with its black-glass backing.
    // Give each laptop its own outward-offset display before world-space batching;
    // keep the bezel and normal depth testing, including in the distant overview.
    display.geometry = geometry(display.geometry.clone());
    const screenPositions = display.geometry.attributes.position;
    const screenNormals = display.geometry.attributes.normal;
    for (let i = 0; i < screenPositions.count; i++) {
      screenPositions.setXYZ(i,
        screenPositions.getX(i) + screenNormals.getX(i) * .004,
        screenPositions.getY(i) + screenNormals.getY(i) * .004,
        screenPositions.getZ(i) + screenNormals.getZ(i) * .004);
    }
    screenPositions.needsUpdate = true;
    display.geometry.computeBoundingBox(); display.geometry.computeBoundingSphere();
    const surface = document.createElement('canvas'); surface.width = 1024; surface.height = 640;
    const ctx = surface.getContext('2d');
    if (!ctx) throw new Error('The laptop screen could not be drawn');
    ctx.fillStyle = '#0d0914'; ctx.fillRect(0, 0, 1024, 640);
    ctx.fillStyle = '#1c1428'; ctx.fillRect(0, 0, 1024, 66);
    ctx.fillStyle = '#f6f6f3'; ctx.font = '600 27px Avenir Next, Avenir, system-ui, sans-serif'; ctx.fillText('DUEN', 35, 43);
    ctx.fillStyle = '#b9adca'; ctx.font = '18px Avenir Next, Avenir, system-ui, sans-serif'; ctx.fillText('Connected chess', 143, 42);
    ctx.fillStyle = '#9bc6ae'; ctx.beginPath(); ctx.arc(844, 33, 5, 0, Math.PI * 2); ctx.fill(); ctx.fillText('Pi connected', 860, 41);
    const ox = 43, oy = 108, cell = 58;
    const back = ['♜', '♞', '♝', '♛', '♚', '♝', '♞', '♜'];
    const draw = (file, rank, white, glyph = '♟') => {
      ctx.font = '48px Georgia, serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.lineWidth = 1.6; ctx.strokeStyle = white ? '#1f162a' : '#ece6f4'; ctx.fillStyle = white ? '#f6f4ec' : '#17111e';
      ctx.strokeText(glyph, ox + (file + .5) * cell, oy + (rank + .51) * cell);
      ctx.fillText(glyph, ox + (file + .5) * cell, oy + (rank + .51) * cell);
    };
    for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
      ctx.fillStyle = (r + f) % 2 ? '#4b3a5e' : '#d3c8dc'; ctx.fillRect(ox + f * cell, oy + r * cell, cell, cell);
    }
    ctx.fillStyle = '#b8db8b77'; ctx.fillRect(ox + 4 * cell, oy + 4 * cell, cell, cell);
    if (confirmed) ctx.fillRect(ox + 4 * cell, oy + 3 * cell, cell, cell);
    for (let f = 0; f < 8; f++) {
      draw(f, 0, false, back[f]); draw(f, 7, true, back[f]);
      draw(f, f === 4 && confirmed ? 3 : 1, false); draw(f, f === 4 ? 4 : 6, true);
    }
    ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
    ctx.fillStyle = '#f6f6f3'; ctx.font = '600 29px Avenir Next, Avenir, system-ui, sans-serif';
    ctx.fillText(confirmed ? 'Move confirmed' : 'Your opening move', 560, 154);
    ctx.fillStyle = '#a89ab7'; ctx.font = '19px Avenir Next, Avenir, system-ui, sans-serif';
    ctx.fillText(confirmed ? 'Your next move awaits.' : 'Choose a square. Make it count.', 560, 192);
    ctx.fillStyle = '#291c39'; ctx.beginPath(); ctx.roundRect(554, 245, 418, 121, 12); ctx.fill();
    ctx.fillStyle = '#ede1fb'; ctx.font = '500 46px Avenir Next, Avenir, system-ui, sans-serif'; ctx.fillText(confirmed ? 'e7 → e5' : 'e2 → e4', 589, 301);
    ctx.fillStyle = '#c0add3'; ctx.font = '17px Avenir Next, Avenir, system-ui, sans-serif'; ctx.fillText(confirmed ? 'An example Stockfish reply' : 'Your move', 590, 339);
    ctx.fillStyle = '#4d3b5e'; ctx.fillRect(560, 409, 410, 1);
    ctx.fillStyle = '#d8cfdf'; ctx.font = '20px Avenir Next, Avenir, system-ui, sans-serif'; ctx.fillText('1.  e4', 563, 451); if (confirmed) ctx.fillText('e5', 780, 451);
    ctx.fillStyle = '#9b8aa9'; ctx.font = '17px Avenir Next, Avenir, system-ui, sans-serif'; ctx.fillText('One game. Every device.', 560, 540);
    const map = new THREE.CanvasTexture(surface); map.flipY = true; map.colorSpace = THREE.SRGBColorSpace;
    map.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());
    const original = display.material.map;
    if (original) { map.offset.copy(original.offset); map.repeat.copy(original.repeat); map.rotation = original.rotation; map.center.copy(original.center); }
    textures.add(map); display.material = material({ map, toneMapped: false,
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2 }, true);
  }

  function separateBoardPawn(board) {
    const pawn = board.getObjectByName('piece_pawn_white_04');
    if (!pawn || !pawn.parent) throw new Error('The downloaded chess set is missing its opening pawn');
    board.updateWorldMatrix(true, true);
    const from = pawn.parent.localToWorld(pawn.position.clone());
    const to = pawn.parent.localToWorld(pawn.position.clone().add(new THREE.Vector3(0, 0, .115776)));
    const world = pawn.matrixWorld.clone();
    pawn.removeFromParent(); world.decompose(pawn.position, pawn.quaternion, pawn.scale);
    pawn.traverse(object => {
      if (object.isMesh) { object.geometry = object.geometry.clone(); geometries.add(object.geometry); }
    });
    scene.add(pawn);
    const anchorOffset = new THREE.Box3().setFromObject(pawn).getCenter(new THREE.Vector3()).sub(from);
    movingPawn = { object: pawn, from, to, anchorOffset };
  }

  function batchModels(root) {
    root.updateMatrixWorld(true);
    const batches = new Map(), sources = new Set();
    root.children.forEach(device => { featureAnchors.push(device.userData.features.map(feature => feature.object.localToWorld(feature.point.clone()))); });
    root.traverse(object => {
      if (!object.isMesh) return;
      if (object.isSkinnedMesh) throw new Error('This device model requires an animated rig');
      sources.add(object.geometry);
      const source = object.geometry.index ? object.geometry.toNonIndexed() : object.geometry.clone();
      if (!source.attributes.normal) source.computeVertexNormals();
      const surfaces = Array.isArray(object.material) ? object.material : [object.material];
      const groups = Array.isArray(object.material) ? source.groups : [{ start: 0, count: source.attributes.position.count, materialIndex: 0 }];
      groups.forEach(group => {
        const surface = surfaces[group.materialIndex];
        if (!surface) return;
        const part = geometrySlice(source, group.start, group.count).applyMatrix4(object.matrixWorld);
        // Keep UV sets, vertex colors and tangents from the downloaded asset intact.
        const layout = Object.entries(part.attributes).map(([name, attribute]) => `${name}:${attribute.itemSize}:${attribute.array.constructor.name}`).sort().join('|');
        const key = object.userData.storyStation + ':' + surface.uuid + ':' + layout;
        if (!batches.has(key)) batches.set(key, { surface, parts: [] });
        batches.get(key).parts.push(part);
      });
      source.dispose();
    });
    for (const { surface, parts } of batches.values()) {
      const combined = mergeGeometries(parts, false);
      if (!combined) throw new Error('Device geometry could not be combined');
      scene.add(new THREE.Mesh(geometry(combined), surface)); parts.forEach(part => part.dispose());
    }
    sources.forEach(source => { source.dispose(); geometries.delete(source); });
  }

  function makeRoutes() {
    const curve = (startX, endX, depth) => new THREE.CubicBezierCurve3(
      new THREE.Vector3(startX, .03, 1.68), new THREE.Vector3(startX + (endX - startX) * .3, -.18, depth),
      new THREE.Vector3(startX + (endX - startX) * .7, -.18, depth), new THREE.Vector3(endX, .03, 1.68));
    routes = [curve(0, 8, 2.9), curve(8, 16, 2.9), curve(16, 24, 3.5), curve(24, 16, 3.5), curve(16, 8, 2.9), curve(8, 32, 4.8)];
    const quiet = material({ color: '#7a618f', toneMapped: false }, true);
    const flow = material({ color: '#e4d2ff', toneMapped: false }, true);
    const baseParts = [];
    routeMeshes = routes.map((path, i) => {
      const tube = geometry(new THREE.TubeGeometry(path, i === 5 ? 128 : 64, .025, 8, false));
      if (i !== 3 && i !== 4) baseParts.push(tube.clone()); // Replies retrace the board/Pi and Pi/relay connections.
      const line = new THREE.Mesh(new THREE.TubeGeometry(path, i === 5 ? 128 : 64, .031, 8, false), flow); geometries.add(line.geometry);
      line.geometry.setDrawRange(0, 0); scene.add(line); return line;
    });
    scene.add(new THREE.Mesh(geometry(mergeGeometries(baseParts, false)), quiet));
    baseParts.forEach(part => part.dispose());
    const arrows = new THREE.InstancedMesh(geometry(new THREE.ConeGeometry(.10, .28, 12)), flow, routes.length);
    const up = new THREE.Vector3(0, 1, 0);
    routes.forEach((path, i) => {
      path.getPoint(.61, point); quaternion.setFromUnitVectors(up, path.getTangent(.61).normalize());
      scale.setScalar(1); matrix.compose(point, quaternion, scale); arrows.setMatrixAt(i, matrix);
    });
    quaternion.identity(); scene.add(arrows);
    packet = new THREE.Mesh(geometry(new THREE.SphereGeometry(.068, 16, 10)), material({ color: '#fff8ff', toneMapped: false }, true));
    scene.add(packet);
  }

  async function initialize() {
    if (initialized || disposed || motionQuery.matches || failed) return;
    initialized = true; canvas.dataset.renderState = 'loading';
    try {
      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: 'low-power' });
      renderer.setClearColor('#030205', 0); renderer.shadowMap.enabled = false;
      renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.05;
      scene = new THREE.Scene(); camera = new THREE.PerspectiveCamera(36, width / height, .5, 240);
      const environment = new RoomEnvironment(), pmrem = new THREE.PMREMGenerator(renderer);
      environmentTarget = pmrem.fromScene(environment, .03); scene.environment = environmentTarget.texture; scene.environmentIntensity = .6;
      environment.dispose(); pmrem.dispose();
      scene.add(new THREE.HemisphereLight('#f5edf9', '#302139', 1.3));
      const key = new THREE.DirectionalLight('#fff3e7', 2.3); key.position.set(-3, 7, 6); scene.add(key);
      const rim = new THREE.DirectionalLight('#bfa0f2', 1.4); rim.position.set(8, 5, -4); scene.add(rim);
      const face = new THREE.DirectionalLight('#e7ecf8', .8); face.position.set(1, 2, 8); scene.add(face);
      const [laptop, gateway, pi, board] = await Promise.all([
        loadModel(modelOptions.laptopUrl), loadModel(modelOptions.serverUrl), loadModel(modelOptions.piUrl), loadModel(modelOptions.boardUrl),
      ]);
      if (disposed) return;
      const laptopFeatures = [[.77, .91, .12], [.78, .59, .18]];
      const all = new THREE.Group();
      [prepareModel(laptop, modelOptions.laptopRotation || [0, 0, 0], [4.4, 3.1, 3], laptopFeatures),
        prepareModel(gateway, modelOptions.serverRotation || [0, 0, 0], [3.3, 3.2, 2.7], [[.7, .83, .99], [.72, .35, .99]]),
        prepareModel(pi, modelOptions.piRotation || [0, -Math.PI / 2, 0], [4.2, 1.5, 3], [[.5022, .3466, .4529], [.2, .6306, .9941]]),
        prepareModel(board, modelOptions.boardRotation || [0, Math.PI, 0], [4.5, 2.1, 4.5], [[.6, .85, .6], [.5, .18, .99]]),
        prepareModel(laptop, modelOptions.laptopRotation || [0, 0, 0], [4.4, 3.1, 3], [[.3, .63, .2], [.79, .8, .14]])].forEach((device, i) => {
          device.position.x = i * 8; device.traverse(object => { object.userData.storyStation = i; });
          if (i === 0 || i === 4) applyLaptopScreen(device, i === 4); all.add(device);
        });
      all.updateMatrixWorld(true); separateBoardPawn(all.children[3]);
      batchModels(all); modelCache.clear(); makeRoutes(); measure(); progress = target; pose(progress);
      await renderer.compileAsync(scene, camera);
      if (disposed) return;
      renderer.render(scene, camera); frameCount++; // Warm textures and vertex buffers before the stage enters view.
      ready = true; section.classList.add('is-ready'); canvas.dataset.state = 'ready';
      dirty = true; requestFrame();
    } catch (error) {
      failed = true; modelCache.clear(); canvas.dataset.state = 'fallback'; exposeStatic('fallback');
      console.error('The device walkthrough is unavailable:', error);
    }
  }

  function cancelFrame(reason) {
    if (raf) cancelAnimationFrame(raf);
    raf = 0; previousTime = 0; setData(canvas, 'renderState', reason); if (ready) publishMetrics(performance.now(), true);
  }
  function requestFrame() {
    if (ready && !disposed && !document.hidden && pageActive && inView && !motionQuery.matches && !raf) raf = requestAnimationFrame(frame);
  }
  function measure() {
    if (disposed) return;
    const bounds = stage.getBoundingClientRect();
    width = Math.max(1, bounds.width); height = Math.max(1, bounds.height);
    viewportHeight = window.innerHeight; callouts.setAttribute('viewBox', `0 0 ${width} ${height}`);
    start = section.getBoundingClientRect().top + window.scrollY;
    range = Math.max(1, section.offsetHeight - height);
    if (renderer) {
      const ratio = Math.min(window.devicePixelRatio || 1, 1.5, Math.sqrt(1350000 / (width * height)));
      renderer.setPixelRatio(ratio); renderer.setSize(width, height, false);
      camera.aspect = width / height; camera.updateProjectionMatrix();
      canvas.dataset.pixelRatio = ratio.toFixed(2);
      canvas.dataset.bufferPixels = String(Math.round(width * height * ratio * ratio));
    }
    dirty = true; update();
  }
  function update() {
    if (disposed) return;
    const offset = window.scrollY - start;
    target = clamp(offset / range, 0, 1);
    inView = offset < range + height && offset + viewportHeight > 0;
    if (motionQuery.matches) return;
    if (inView && !initialized) initialize();
    if (inView) requestFrame(); else cancelFrame('offscreen');
  }

  function pose(p) {
    const mobile = width < 760;
    const travel = clamp(p / .78, 0, 1) * 4;
    const station = Math.min(3, Math.floor(travel));
    const focus = travel >= 4 ? 4 : station + smooth(.42, .94, travel - station);
    const zoom = smooth(.81, .96, p);
    const tangent = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const aspect = width / height;
    const closeDistance = Math.max(9.5, 4.8 / (2 * tangent * aspect * (mobile ? .9 : .57)), 3.35 / (2 * tangent * (mobile ? .44 : .66)));
    const overviewDistance = Math.max(18, 38 / (2 * tangent * aspect * .89));
    const distance = mix(closeDistance, overviewDistance, zoom);
    const topView = Math.max(1 - smooth(.15, .75, Math.abs(focus - 2)), 1 - smooth(.15, .75, Math.abs(focus - 3)));
    targetPoint.set(mix(focus * 8, 16, zoom), mix(mix(1.05, .55, topView), 1, zoom), 0);
    const elevation = mix(.33 + .24 * topView, .36, zoom);
    position.set(mix(.12, .025, zoom), elevation, 1).normalize().multiplyScalar(distance).add(targetPoint);
    camera.position.copy(position); camera.lookAt(targetPoint);
    camera.setViewOffset(width, height, -width * mix(mobile ? 0 : .16, 0, zoom), -height * mix(mobile ? .18 : .07, .08, zoom), width, height);
    camera.updateMatrixWorld();
    if (movingPawn) movingPawn.object.position.lerpVectors(movingPawn.from, movingPawn.to, smooth(.59, .66, p));
    // The board leg previews planned motor control; confirmed replies still return via the Pi and relay.
    // Same protected route in both directions; the last station is the original browser again.
    const boundaries = [0, .18, .38, .56, .65, .70, .78];
    let routeIndex = 5, routeProgress = 1;
    routeMeshes.forEach((line, i) => {
      const t = clamp((p - boundaries[i]) / (boundaries[i + 1] - boundaries[i]), 0, 1);
      const count = line.geometry.index.count;
      line.geometry.setDrawRange(0, Math.floor(count * t / 6) * 6);
      if (p >= boundaries[i] && p < boundaries[i + 1]) { routeIndex = i; routeProgress = t; }
    });
    packet.visible = p > .003 && p < .80;
    routes[routeIndex].getPoint(routeProgress, packet.position);
    const phase = zoom > .45 ? 'overview' : phases[Math.round(focus)];
    setData(canvas, 'phase', phase); setData(section, 'phase', phase);
    setData(section, 'activeStep', zoom > .45 ? 'all' : String(Math.round(focus)));
    updateCopy(p, focus, zoom); updateCallouts(focus, zoom, mobile);
    labels.forEach((element, index) => {
      const node = Number(element.dataset.node ?? index);
      point.set(node * 8, -.25, 1.55).project(camera);
      const x = (point.x * .5 + .5) * width, y = (-point.y * .5 + .5) * height;
      const labelY = Math.max(y + 28, height * (mobile ? .65 : .73));
      element.style.transform = `translate(${x.toFixed(1)}px,${labelY.toFixed(1)}px) translate(-50%,0)`;
      element.style.opacity = String(smooth(.87, .96, p));
    });
  }

  function updateCallouts(focus, zoom, mobile) {
    const station = Math.round(focus), distance = Math.abs(focus - station);
    const opacity = (1 - smooth(.08, .24, distance)) * (1 - smooth(0, .13, zoom));
    callouts.style.display = ''; callouts.style.opacity = opacity.toFixed(3);
    if (opacity < .005 || !featureAnchors[station]) return;
    if (station !== calloutStation) {
      calloutParts.forEach((part, index) => { part.label.textContent = calloutNames[station][index]; });
      calloutStation = station;
    }
    calloutParts.forEach((part, index) => {
      part.group.style.display = mobile && index === 1 ? 'none' : '';
      if (mobile && index === 1) return;
      if (station === 3 && index === 0 && movingPawn) {
        point.copy(movingPawn.object.position).add(movingPawn.anchorOffset);
      } else point.copy(featureAnchors[station][index]);
      point.project(camera);
      const x = (point.x * .5 + .5) * width, y = (-point.y * .5 + .5) * height;
      // Two deliberate leader segments; keep labels clear of the left-hand story and top navigation.
      const labelX = width * (mobile ? .94 : .93);
      const labelY = mobile ? height * .57 : Math.max(156, height * (index === 0 ? .34 : .79));
      const elbowX = Math.min(labelX - 24, Math.max(x + 30, width * (mobile ? .7 : .77)));
      part.path.setAttribute('d', `M ${x.toFixed(1)} ${y.toFixed(1)} L ${elbowX.toFixed(1)} ${labelY.toFixed(1)} L ${labelX.toFixed(1)} ${labelY.toFixed(1)}`);
      part.circle.setAttribute('cx', x.toFixed(1)); part.circle.setAttribute('cy', y.toFixed(1));
      part.label.setAttribute('x', labelX.toFixed(1)); part.label.setAttribute('y', (labelY - 8).toFixed(1));
    });
  }

  function setData(element, key, value) { if (element.dataset[key] !== value) element.dataset[key] = value; }
  function publishMetrics(now, force = false) {
    if (!renderer || (!force && now - lastMetricsTime < 1000)) return;
    lastMetricsTime = now;
    setData(canvas, 'frames', String(frameCount)); setData(canvas, 'renderFrames', String(frameCount));
    setData(canvas, 'progress', progress.toFixed(4));
    setData(canvas, 'drawCalls', String(renderer.info.render.calls)); setData(canvas, 'triangles', String(renderer.info.render.triangles));
    setData(canvas, 'shadowPasses', '0');
  }

  function updateCopy(p, focus, zoom) {
    const active = zoom > .24 ? 5 : Math.round(focus);
    steps.forEach((element, index) => {
      const step = Number(element.dataset.step ?? index);
      const opacity = (1 - smooth(.12, .48, Math.abs(focus - step))) * (1 - smooth(.03, .22, zoom));
      element.style.opacity = opacity.toFixed(4);
      if (activeCopy !== active) { const hidden = step !== active; element.inert = hidden; element.setAttribute('aria-hidden', String(hidden)); }
    });
    if (overview) {
      overview.style.opacity = smooth(.26, .52, zoom).toFixed(4);
      if (activeCopy !== active) { overview.inert = active !== 5; overview.setAttribute('aria-hidden', String(active !== 5)); }
    }
    if (activeCopy !== active) labels.forEach(element => element.setAttribute('aria-hidden', String(active !== 5)));
    activeCopy = active;
  }

  function frame(now) {
    raf = 0;
    if (disposed || document.hidden || !pageActive || !inView || motionQuery.matches) { cancelFrame(document.hidden ? 'hidden' : 'offscreen'); return; }
    const dt = previousTime ? Math.min((now - previousTime) / 1000, .05) : 1 / 60; previousTime = now;
    progress = mix(progress, target, 1 - Math.exp(-13 * dt));
    if (Math.abs(progress - target) < .00005) progress = target;
    if (dirty || progress !== lastProgress) {
      pose(progress); renderer.render(scene, camera); frameCount++;
      publishMetrics(now, progress === target); dirty = false; lastProgress = progress;
    }
    if (progress !== target) { setData(canvas, 'renderState', 'scrolling'); requestFrame(); }
    else cancelFrame('idle');
  }

  function onVisibility() {
    if (document.hidden) cancelFrame('hidden'); else { dirty = true; measure(); }
  }
  function onHide() { pageActive = false; cancelFrame('hidden'); }
  function onShow() { pageActive = true; dirty = true; measure(); }
  function onMotion() {
    if (motionQuery.matches) exposeStatic('reduced');
    else { section.classList.remove('is-reduced'); if (!failed) section.classList.remove('is-fallback'); activeCopy = -2; dirty = true; measure(); }
  }
  const observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting) && !motionQuery.matches) initialize();
  }, { rootMargin: '600px 0px' });
  observer.observe(section);
  const resizeObserver = new ResizeObserver(measure); resizeObserver.observe(stage); resizeObserver.observe(section);
  window.addEventListener('scroll', update, { passive: true }); window.addEventListener('resize', measure);
  window.addEventListener('pagehide', onHide); window.addEventListener('pageshow', onShow);
  document.addEventListener('visibilitychange', onVisibility); motionQuery.addEventListener('change', onMotion);
  canvas.addEventListener('webglcontextlost', onContextLost);
  function onContextLost(event) { event.preventDefault(); failed = true; ready = false; exposeStatic('fallback'); }
  canvas.dataset.frames = '0'; canvas.dataset.renderFrames = '0'; canvas.dataset.state = 'waiting'; canvas.dataset.renderState = 'waiting';
  if (motionQuery.matches) { canvas.dataset.state = 'reduced'; exposeStatic('reduced'); }
  measure();

  return function destroy() {
    disposed = true; cancelFrame('disposed'); observer.disconnect(); resizeObserver.disconnect();
    window.removeEventListener('scroll', update); window.removeEventListener('resize', measure);
    window.removeEventListener('pagehide', onHide); window.removeEventListener('pageshow', onShow);
    document.removeEventListener('visibilitychange', onVisibility); motionQuery.removeEventListener('change', onMotion);
    canvas.removeEventListener('webglcontextlost', onContextLost);
    geometries.forEach(item => item.dispose()); materials.forEach(item => item.dispose()); textures.forEach(item => item.dispose());
    environmentTarget?.dispose(); renderer?.dispose(); callouts.remove(); delete section.dataset.howMounted;
  };
}

mountHowItWorks();
