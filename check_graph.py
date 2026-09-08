import json
with open('docs/function_graph.json', 'r', encoding='utf-8') as f:
    graph = json.load(f)

for node in graph.get('nodes', []):
    if node.get('id') == 'one_click_update':
        print('=== one_click_update 节点完整描述 ===')
        print(node.get('description', ''))
        print()
        print('=== 相关边 ===')
        for edge in graph.get('edges', []):
            src = edge.get('source', '')
            tgt = edge.get('target', '')
            if 'one_click' in src or 'one_click' in tgt:
                label = edge.get('label', '')
                print(f'  {src} -> {tgt}: {label}')
