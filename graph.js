fetch('/test-orinoco-downstream-website/graph.json?v=d7be3d1a1dac817730d34df67537cc8220b5db9d31024fef3141f09ee42d9f1b')
  .then((response) => {
    if (!response.ok) {
      throw new Error(`Could not load graph: ${response.status}`);
    }
    return response.json();
  })
  .then((graph) => {
    window.orinocoGraph = graph;
    window.dispatchEvent(new CustomEvent('orinoco:graph-ready', { detail: graph }));
    const target = document.getElementById('orinoco-graph');
    if (target) {
      target.dataset.nodes = String(graph.nodes?.length ?? 0);
      target.dataset.edges = String(graph.edges?.length ?? 0);
    }
  })
  .catch((error) => {
    console.error(error);
  });
