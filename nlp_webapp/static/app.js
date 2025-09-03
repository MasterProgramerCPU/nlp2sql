let cy = null;

function setGraphLoading(flag) {
  const el = document.getElementById('cy');
  if (!el) return;
  el.classList.toggle('is-loading', !!flag);
  if (flag) {
    let sp = el.querySelector('.spinner-abs');
    if (!sp) { sp = document.createElement('div'); sp.className = 'spinner-abs'; el.appendChild(sp); }
  } else {
    const sp = el.querySelector('.spinner-abs');
    if (sp) sp.remove();
  }
}

function computeFkMaps(fks) {
  const srcMap = {}, dstMap = {};
  (fks || []).forEach(fk => {
    const src = `${fk.src_schema}.${fk.src_table}`;
    const dst = `${fk.dst_schema}.${fk.dst_table}`;
    (srcMap[src] = srcMap[src] || new Set()).add(fk.src_column);
    (dstMap[dst] = dstMap[dst] || new Set()).add(fk.dst_column);
  });
  return { srcMap, dstMap };
}

function tableLabel(key, tableObj, showColumns, fkInfo) {
  if (!showColumns) return key;
  const cols = (tableObj.columns || []).map(c => {
    const name = c.name ?? c[0];
    const type = c.type ?? c[1];
    const l = (fkInfo.srcMap[key] && fkInfo.srcMap[key].has(name)) ? '↗ ' : '  ';
    const r = (fkInfo.dstMap[key] && fkInfo.dstMap[key].has(name)) ? ' ↘' : '';
    return `${l}${name}: ${type}${r}`;
  });
  return `${key}\n────\n${cols.join("\n")}`;
}

function buildElements(schema, options) {
  const elements = [];
  const tables = schema.tables || {};
  const fks = schema.fks || [];
  const fkInfo = computeFkMaps(fks);

  Object.keys(tables).forEach(key => {
    const label = tableLabel(key, tables[key], options.showColumns, fkInfo);
    elements.push({
      data: { id: key, label },
      classes: (options.focus && key === options.focus) ? 'focus' : ''
    });
  });

  fks.forEach(fk => {
    const src = `${fk.src_schema}.${fk.src_table}`;
    const dst = `${fk.dst_schema}.${fk.dst_table}`;
    if (!tables[src] || !tables[dst]) return;
    elements.push({
      data: {
        id: `e:${src}.${fk.src_column}->${dst}.${fk.dst_column}`,
        source: src, target: dst,
        label: options.showEdgeLabels ? `${fk.src_column} → ${fk.dst_column}` : ''
      }
    });
  });

  return elements;
}

function dagreLayout() {
  return { name: 'dagre', rankDir: 'LR', nodeSep: 90, edgeSep: 40, rankSep: 210, fit: true, padding: 70 };
}

async function renderDiagram() {
  const focus = (document.getElementById('focus-input')?.value || '').trim();
  const showColumns = document.getElementById('toggle-columns')?.checked ?? true;
  const showEdgeLabels = document.getElementById('toggle-edge-labels')?.checked ?? true;

  setGraphLoading(true);
  let data;
  try {
    // RELATIV, nu absolut: se respectă prefixul când app-ul e montat sub /apps/nl2sql
    const res = await fetch('schema.json');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    console.error('schema.json error', e);
    setGraphLoading(false);
    return;
  }

  const elements = buildElements(data, { focus, showColumns, showEdgeLabels });

  const style = [
    { selector: 'node', style: {
        'shape': 'round-rectangle',
        'background-color': '#151632',
        'border-color': '#ff1d8e',
        'border-width': 3,
        'width': 'label', 'height': 'label',
        'padding': '22px',
        'label': 'data(label)',
        'text-wrap': 'wrap',
        'text-max-width': 560,
        'font-size': 15,
        'min-zoomed-font-size': 8,
        'color': '#f5f7ff',
        'text-valign': 'center',
        'text-halign': 'center',
        'text-outline-color': '#090c1e',
        'text-outline-width': 3,
        'shadow-blur': 24,
        'shadow-color': '#ff1d8e66',
        'shadow-offset-x': 0, 'shadow-offset-y': 0
    }},
    { selector: 'node.focus', style: {
        'border-color': '#00e5ff',
        'border-width': 4,
        'background-color': '#161b3a',
        'shadow-color': '#00e5ff88'
    }},
    { selector: 'edge', style: {
        'curve-style': 'taxi',
        'taxi-direction': 'auto',
        'taxi-turn-min-distance': 28,
        'target-arrow-shape': 'triangle',
        'target-arrow-color': '#ff69b4',
        'line-color': '#ff69b4',
        'width': 3,
        'opacity': 0.96,
        'label': 'data(label)',
        'font-size': 12,
        'min-zoomed-font-size': 9,
        'color': '#ffd3ea',
        'text-background-color': '#0b1220',
        'text-background-opacity': 0.9,
        'text-background-padding': 3,
        'text-outline-color': '#0b1220',
        'text-outline-width': 2
    }},
    { selector: ':selected', style: { 'border-width': 5, 'border-color': '#00ffa3' } }
  ];

  if (!cy) {
    cy = cytoscape({
      container: document.getElementById('cy'),
      elements, style,
      layout: dagreLayout(),
      wheelSensitivity: 0.2,
      pixelRatio: 1,
      nodeDimensionsIncludeLabels: true
    });

    cy.on('dblclick', 'node', (evt) => {
      const id = evt.target.id();
      const inp = document.getElementById('focus-input');
      if (inp) inp.value = id;
      renderDiagram();
    });

    window.addEventListener('resize', () => {
      cy.resize();
      cy.layout(dagreLayout()).run();
    });
  } else {
    cy.elements().remove();
    cy.add(elements);
    cy.layout(dagreLayout()).run();
  }

  setGraphLoading(false);
}

function graphZoom(delta) {
  if (!cy) return;
  const z = cy.zoom();
  const target = Math.max(0.1, Math.min(4, z + delta));
  cy.zoom({ level: target, renderedPosition: { x: cy.width()/2, y: cy.height()/2 } });
}
function graphFit() { if (cy) cy.fit(undefined, 60); }

window.renderDiagram   = renderDiagram;
window.graphZoom       = graphZoom;
window.graphFit        = graphFit;
