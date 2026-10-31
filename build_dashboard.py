"""Build native, responsive JOMOO dashboard; validate all live entity references.
Run inside the HA container; does not call any device service.
"""
import argparse
import json
from pathlib import Path
import yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config', type=Path, default=Path('/config'))
parser.add_argument('--entry-id', help='Required only when multiple JOMOO devices are installed')
parser.add_argument('--output', type=Path, default=Path('/tmp/dashboard-jomoo-redesign.yaml'))
args = parser.parse_args()
CONFIG = args.config
registry = json.loads((CONFIG / '.storage/core.entity_registry').read_text(encoding='utf-8'))
entities = {e['entity_id']: e for e in registry['data']['entities']}
ours = [e for e in entities.values() if e.get('platform') == 'jomoo_zs800i']
entry_ids = {e['config_entry_id'] for e in ours}
if args.entry_id:
    if args.entry_id not in entry_ids:
        parser.error('No matching JOMOO config entry')
    entry_id = args.entry_id
elif len(entry_ids) == 1:
    entry_id = next(iter(entry_ids))
else:
    parser.error('Select one installed JOMOO device with --entry-id')
by_key = {e['entity_id'].split('.')[0] + ':' + e['unique_id'].split('_', 1)[1]: e['entity_id']
          for e in ours if e['config_entry_id'] == entry_id}
used = set()

def eid(key):
    if ':' in key:
        entity_id = by_key[key]
    else:
        matches = [v for k, v in by_key.items() if k.split(':', 1)[1] == key]
        assert len(matches) == 1, (key, matches)
        entity_id = matches[0]
    assert not entities[entity_id].get('disabled_by'), entity_id
    used.add(entity_id)
    return entity_id

def heading(text, icon):
    return {'type': 'heading', 'heading': text, 'icon': icon,
            'heading_style': 'title'}

def tile(key, name, icon, color='blue', width=6, action=None, **extra):
    entity = eid(key)
    card = {'type': 'tile', 'entity': entity, 'name': name, 'icon': icon,
            'color': color, 'vertical': False,
            'grid_options': {'columns': width, 'rows': 1}}
    if action:
        tap = {'action': 'perform-action', 'perform_action': action,
               'target': {'entity_id': entity}}
        card.update(hide_state=True, tap_action=tap, icon_tap_action=tap,
                    hold_action={'action': 'none'}, double_tap_action={'action': 'none'})
    card.update(extra)
    return card

def button(key, name, icon, color='blue', width=6):
    return tile('button:' + key, name, icon, color, width, 'button.press')

def row(key, name, icon):
    return {'entity': eid(key), 'name': name, 'icon': icon}

def section(cards):
    return {'type': 'grid', 'cards': cards}

quick = [heading('日常使用', 'mdi:gesture-tap')]
quick += [button('stop', '停止', 'mdi:stop-circle-outline', 'red'),
          button('refresh_status', '刷新状态', 'mdi:refresh', 'grey'),
          button('wash_hip', '臀洗', 'mdi:shower-head', 'cyan'),
          button('wash_woman', '妇洗', 'mdi:human-female', 'pink'),
          button('dry', '暖风烘干', 'mdi:fan', 'orange'),
          button('flush_large', '大冲水', 'mdi:water', 'blue'),
          button('wash_auto_hip', '自动臀洗', 'mdi:autorenew', 'cyan'),
          button('wash_auto_woman', '自动妇洗', 'mdi:autorenew', 'pink'),
          button('flush_small', '小冲水', 'mdi:water-outline', 'blue'),
          button('disconnect_bluetooth', '释放蓝牙连接', 'mdi:bluetooth-off', 'grey')]

comfort = [heading('舒适温度', 'mdi:thermometer')]
for key, name, icon, color in [
    ('set_water_level', '水温 · 0–5 档', 'mdi:thermometer-water', 'blue'),
    ('set_seat_level', '座温 · 0–5 档', 'mdi:seat', 'deep-orange'),
    ('set_air_level', '风温 · 0–3 档', 'mdi:fan', 'orange'),
]:
    card = tile(key, name, icon, color, 12,
                features=[{'type': 'numeric-input', 'style': 'slider'}],
                features_position='bottom')
    card['grid_options']['rows'] = 2
    comfort.append(card)
comfort.append({'type': 'markdown', 'content': '拖动滑块后松手发送；**0 为关闭加热**。数值以设备回读为准，并非摄氏度。',
                'grid_options': {'columns': 12, 'rows': 'auto'}})
comfort.append({'type': 'entities', 'show_header_toggle': False,
                'entities': [row('last_control_result', '最近控制结果', 'mdi:check-circle-outline'),
                             row('last_status_time', '最近状态更新', 'mdi:clock-outline')],
                'grid_options': {'columns': 12, 'rows': 'auto'}})

lid = [heading('盖板与座圈', 'mdi:toilet'),
       button('lid_open', '打开盖板', 'mdi:arrow-up-bold-box-outline', 'teal'),
       button('lid_close', '关盖关圈', 'mdi:arrow-down-bold-box-outline', 'teal'),
       button('ring_open', '开盖开圈', 'mdi:arrow-up-bold-outline', 'teal'),
       button('ring_close', '开盖关圈', 'mdi:arrow-down-bold-outline', 'teal'),
       {'type': 'entities', 'show_header_toggle': False,
        'entities': [row('occupancy', '着座快照', 'mdi:account'),
                     row('binary_sensor:lid_open', '盖板快照', 'mdi:toilet'),
                     row('binary_sensor:ring_open', '座圈快照', 'mdi:seat-outline')],
        'grid_options': {'columns': 12, 'rows': 'auto'}}]
lid.append({'type': 'markdown',
            'content': '盖圈动作存在联动；开启「关盖冲厕」后，关盖可能同时冲水。',
            'grid_options': {'columns': 12, 'rows': 'auto'}})

settings = [heading('自动功能', 'mdi:tune')]
for key, name, icon, color in [
    ('set_atmosphere_on', '氛围灯', 'mdi:lightbulb-on-outline', 'amber'),
    ('set_foot_sensor', '脚感控制', 'mdi:foot-print', 'teal'),
    ('set_prewet', '预湿润', 'mdi:water-plus', 'blue'),
    ('set_auto_bubble', '自动发泡', 'mdi:chart-bubble', 'cyan'),
    ('set_uv_water', 'UV 杀菌', 'mdi:shield-check-outline', 'purple'),
    ('set_close_flush', '关盖冲厕', 'mdi:water-sync', 'blue'),
    ('set_auto_small_flush', '自动小冲', 'mdi:water-outline', 'blue'),
]:
    settings.append(tile(key.replace('set_', 'control_', 1), name, icon, color,
                         features=[{'type': 'toggle'}]))
    settings[-1]['grid_options']['rows'] = 2

status = {'type': 'entities', 'title': '设备回读记录', 'show_header_toggle': False,
          'entities': [row('last_query_result', '最近查询', 'mdi:database-check-outline'),
                       row('last_control_result', '最近控制', 'mdi:check-circle-outline'),
                       row('last_status_time', '状态快照时间', 'mdi:clock-outline')]}
notes = {'type': 'markdown', 'content': '''### 使用说明
- 主页分为日常使用、舒适温度、盖板与座圈、自动功能；手机会自动单列排列。
- 档位滑块按整数吸附，松手发送一次；0 为关闭对应加热。显示数值来自设备回读。
- 点击动作卡片或图标均发送同一个命令，长按和双击不额外触发动作。
- 打开盖板会保留座圈状态；其他盖圈组合可能联动。
- 默认持续保持蓝牙连接；厂商小程序需要直连时先点击「释放蓝牙连接」，下次 HA 操作才会重新连接。
- 命令写出即返回，回读在后台观察，不挡住下一条；仍保留固件短发送间隔和动作前检查。
- 灯与风温 0/1/2 档已有实机回读验证；其他控制基于 237371 协议，仍需逐项实物核对。
- 状态是最近回读快照，并非持续实时监测。「回读不匹配」代表回读未确认目标值，不应当作成功。
'''}
config = {'title': '九牧马桶', 'views': [
    {'title': '马桶控制', 'path': 'status', 'icon': 'mdi:toilet',
     'type': 'sections', 'max_columns': 2,
     'sections': [section(quick), section(comfort), section(lid), section(settings)]},
    {'title': '状态与说明', 'path': 'details', 'icon': 'mdi:information-outline',
     'cards': [status, notes]},
]}
output = args.output
if output.exists():
    output.read_text(encoding='utf-8')
output.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
assert yaml.safe_load(output.read_text(encoding='utf-8')) == config
print(f'Validated {len(used)} enabled entity references; output: {output}')
