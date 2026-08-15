(() => {
  "use strict";

  const canvas = document.getElementById("graph-canvas");
  const svg = document.getElementById("dependency-svg");
  if (!canvas || !svg) return;

  const nodes = new Map(
    Array.from(canvas.querySelectorAll(".task-node")).map((node) => [node.dataset.taskId, node])
  );
  const details = new Map(
    Array.from(document.querySelectorAll("[data-detail-id]")).map((panel) => [panel.dataset.detailId, panel])
  );
  const edges = Array.from(svg.querySelectorAll(".dependency-edge")).map((path) => ({
    path,
    sourceId: path.dataset.from,
    targetId: path.dataset.to,
  }));
  const connectedEdges = new Map();
  for (const taskId of nodes.keys()) connectedEdges.set(taskId, []);
  for (const edge of edges) {
    connectedEdges.get(edge.sourceId)?.push(edge);
    connectedEdges.get(edge.targetId)?.push(edge);
  }

  let lockedId = nodes.keys().next().value || null;
  let suppressClick = false;

  function setActiveEdges(taskId) {
    for (const edge of edges) {
      edge.path.classList.toggle(
        "is-active",
        edge.sourceId === taskId || edge.targetId === taskId
      );
    }
  }

  function showDetail(taskId) {
    if (!details.has(taskId)) return;
    for (const [id, panel] of details) panel.hidden = id !== taskId;
    for (const [id, node] of nodes) node.setAttribute("aria-pressed", String(id === taskId));
    setActiveEdges(taskId);
  }

  function nodeBox(node) {
    return {
      left: node.offsetLeft,
      top: node.offsetTop,
      width: node.offsetWidth,
      height: node.offsetHeight,
      centerX: node.offsetLeft + node.offsetWidth / 2,
      centerY: node.offsetTop + node.offsetHeight / 2,
    };
  }

  function selectConnectionPoints(source, target) {
    const dx = target.centerX - source.centerX;
    const dy = target.centerY - source.centerY;
    if (Math.abs(dy) >= Math.abs(dx)) {
      return dy >= 0
        ? {
            orientation: "vertical",
            source: { x: source.centerX, y: source.top + source.height },
            target: { x: target.centerX, y: target.top },
          }
        : {
            orientation: "vertical",
            source: { x: source.centerX, y: source.top },
            target: { x: target.centerX, y: target.top + target.height },
          };
    }
    return dx >= 0
      ? {
          orientation: "horizontal",
          source: { x: source.left + source.width, y: source.centerY },
          target: { x: target.left, y: target.centerY },
        }
      : {
          orientation: "horizontal",
          source: { x: source.left, y: source.centerY },
          target: { x: target.left + target.width, y: target.centerY },
        };
  }

  function updateEdge(edge) {
    const sourceNode = nodes.get(edge.sourceId);
    const targetNode = nodes.get(edge.targetId);
    if (!sourceNode || !targetNode) return;
    const points = selectConnectionPoints(nodeBox(sourceNode), nodeBox(targetNode));
    const source = points.source;
    const target = points.target;
    let path;
    if (points.orientation === "vertical") {
      const middle = (source.y + target.y) / 2;
      path = `M ${source.x} ${source.y} C ${source.x} ${middle}, ${target.x} ${middle}, ${target.x} ${target.y}`;
    } else {
      const middle = (source.x + target.x) / 2;
      path = `M ${source.x} ${source.y} C ${middle} ${source.y}, ${middle} ${target.y}, ${target.x} ${target.y}`;
    }
    edge.path.setAttribute("d", path);
  }

  function updateConnectedEdges(taskId) {
    for (const edge of connectedEdges.get(taskId) || []) updateEdge(edge);
  }

  function updateAllEdges() {
    for (const edge of edges) updateEdge(edge);
  }

  function syncSvgSize() {
    const width = Math.max(canvas.offsetWidth, canvas.scrollWidth);
    const height = Math.max(canvas.offsetHeight, canvas.scrollHeight);
    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  }

  function expandCanvasFor(node) {
    const requiredWidth = node.offsetLeft + node.offsetWidth + 72;
    const requiredHeight = node.offsetTop + node.offsetHeight + 72;
    const currentWidth = parseFloat(canvas.style.width) || canvas.offsetWidth;
    const currentHeight = parseFloat(canvas.style.height) || canvas.offsetHeight;
    if (requiredWidth > currentWidth) canvas.style.width = `${Math.ceil(requiredWidth)}px`;
    if (requiredHeight > currentHeight) canvas.style.height = `${Math.ceil(requiredHeight)}px`;
    syncSvgSize();
  }

  function installDragging(node) {
    let drag = null;
    let frame = null;

    function paint() {
      frame = null;
      if (!drag || !drag.moved) return;
      node.style.left = `${Math.max(0, drag.left + drag.dx)}px`;
      node.style.top = `${Math.max(0, drag.top + drag.dy)}px`;
      expandCanvasFor(node);
      updateConnectedEdges(node.dataset.taskId);
    }

    node.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      drag = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        left: node.offsetLeft,
        top: node.offsetTop,
        dx: 0,
        dy: 0,
        moved: false,
      };
      node.setPointerCapture(event.pointerId);
    });

    node.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      drag.dx = event.clientX - drag.startX;
      drag.dy = event.clientY - drag.startY;
      if (!drag.moved && Math.hypot(drag.dx, drag.dy) < 3) return;
      drag.moved = true;
      node.classList.add("is-dragging");
      event.preventDefault();
      if (frame === null) frame = requestAnimationFrame(paint);
    });

    function finishDrag(event) {
      if (!drag || event.pointerId !== drag.pointerId) return;
      if (frame !== null) {
        cancelAnimationFrame(frame);
        paint();
      }
      if (node.hasPointerCapture(event.pointerId)) node.releasePointerCapture(event.pointerId);
      node.classList.remove("is-dragging");
      if (drag.moved) {
        suppressClick = true;
        lockedId = node.dataset.taskId;
        showDetail(lockedId);
        setTimeout(() => { suppressClick = false; }, 0);
      }
      drag = null;
    }

    node.addEventListener("pointerup", finishDrag);
    node.addEventListener("pointercancel", finishDrag);
  }

  for (const [taskId, node] of nodes) {
    installDragging(node);
    node.addEventListener("pointerenter", () => showDetail(taskId));
    node.addEventListener("pointerleave", () => { if (lockedId) showDetail(lockedId); });
    node.addEventListener("focus", () => showDetail(taskId));
    node.addEventListener("blur", () => { if (lockedId) showDetail(lockedId); });
    node.addEventListener("click", () => {
      if (suppressClick) return;
      lockedId = taskId;
      showDetail(taskId);
    });
  }

  syncSvgSize();
  updateAllEdges();
  if (lockedId) showDetail(lockedId);
  window.addEventListener("resize", () => {
    syncSvgSize();
    updateAllEdges();
  });
})();
