"""List all WAN-related nodes in ComfyUI"""
import sys
sys.path.insert(0, r".\ComfyUI")
import nodes

all_nodes = list(nodes.NODE_CLASS_MAPPINGS.keys())
wan_nodes = [n for n in all_nodes if 'wan' in n.lower()]
print(f"Total nodes: {len(all_nodes)}")
print(f"\nWAN nodes ({len(wan_nodes)}):")
for n in sorted(wan_nodes):
    print(f"  {n}")

# Also check for video-related nodes
video_nodes = [n for n in all_nodes if 'video' in n.lower() or 'i2v' in n.lower()]
print(f"\nVideo nodes ({len(video_nodes)}):")
for n in sorted(video_nodes)[:20]:
    print(f"  {n}")
