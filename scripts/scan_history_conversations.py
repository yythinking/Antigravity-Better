#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity 历史全量会话深度扫描与 Token 计费导出脚本 (跨平台全功能版)
---------------------------------------------------------------------
功能：
1. 跨平台自动定位 Antigravity 历史存储目录（支持 Linux / macOS / Windows）
2. 深度扫描 SQLite 数据库 (`conversations/*.db`) 与日志 (`transcript.jsonl`)
3. 仅在模型来源可可靠识别时按模型计价，未知模型不静默套用默认价格
4. 包含完整思考链过程（Thinking Tokens）；在缺少底层 Provider Usage 元数据时，对 2k 以上物理在席上下文采用 72% 启发式前缀缓存估算（并明确标注 estimated），绝不谎称为官方精确用量
5. 生成可供前端直接一键「覆盖导入」的标准化 JSON 明细文件
"""

import os
import sys
import argparse
import glob
import json
import sqlite3
import re
from datetime import datetime
from collections import Counter

# 模型费率/估算表 (每 100 万 Tokens 美元价格)
MODEL_PRICING_REGISTRY = {
    'gemini-3.1-pro': {
        'id': 'gemini-3.1-pro',
        'name': 'Gemini 3.1 Pro',
        'family': 'pro',
        'input': 2.00,
        'output': 12.00,
        'cache': 0.20,
        'input_200k': 4.00,
        'output_200k': 18.00,
        'cache_200k': 0.40
    },
    'gemini-3.8-flash': {
        'id': 'gemini-3.8-flash',
        'name': 'Gemini 3.8 Flash',
        'family': 'flash',
        'input': 0.75,
        'output': 3.75,
        'cache': 0.075
    },
    'gemini-3.7-flash': {
        'id': 'gemini-3.7-flash',
        'name': 'Gemini 3.7 Flash',
        'family': 'flash',
        'input': 0.75,
        'output': 3.75,
        'cache': 0.075
    },
    'gemini-3.6-flash': {
        'id': 'gemini-3.6-flash',
        'name': 'Gemini 3.6 Flash',
        'family': 'flash',
        'input': 0.75,
        'output': 3.75,
        'cache': 0.075
    },
    'claude-sonnet-4.6': {
        'id': 'claude-sonnet-4.6',
        'name': 'Claude Sonnet 4.6',
        'family': 'claude',
        'input': 3.00,
        'output': 15.00,
        'cache': 0.30,
        'cacheWrite': 3.75
    },
    'claude-opus-4.6': {
        'id': 'claude-opus-4.6',
        'name': 'Claude Opus 4.6',
        'family': 'claude',
        'input': 5.00,
        'output': 25.00,
        'cache': 0.50,
        'cacheWrite': 6.25
    },
    'gpt-oss-120b': {
        'id': 'gpt-oss-120b',
        'name': 'GPT-OSS 120B (Medium)',
        'family': 'oss',
        'input': 2.50,
        'output': 10.00,
        # Antigravity 和 AG-Monitor-Pro 都没有公开独立缓存费率；
        # 仅用输入估算值填充 schema，脚本不会从 transcript 推断缓存命中。
        'cache': 2.50,
        'cacheWrite': 2.50
    }
}

TOKEN_SYMBOLS = frozenset("{}[]:;,.<>?/|=+-*&^%$#@!~`'\"()_\\")

def normalize_model_id(raw_str):
    """将 SQLite 元数据或日志中的原始字符串映射为标准模型 ID"""
    if not raw_str:
        return None
    s = str(raw_str).lower().strip()
    # 只接受完整的、当前项目实际配置的模型标识。不能看到一个泛化的
    # ``flash``/``pro``/``claude`` 就猜价格，否则未知模型会被错价。
    model_sep = r'[-._ ]*'
    # 空格后的 Preview/Thinking 等也是变体后缀；但 ``(High)`` 这类
    # UI 展示标记不应被排除，因此只阻止分隔符后直接跟字母数字。
    suffix_boundary = r'(?![-._\s]+[a-z0-9])(?!\s*\(\s*(?!high\s*\))[a-z])'
    if re.search(rf'(?<![a-z0-9])claude{model_sep}opus{model_sep}4{model_sep}6{suffix_boundary}', s):
        return 'claude-opus-4.6'
    if re.search(rf'(?<![a-z0-9])claude{model_sep}sonnet{model_sep}4{model_sep}6{suffix_boundary}', s):
        return 'claude-sonnet-4.6'
    if re.search(rf'(?<![a-z0-9])gemini{model_sep}3{model_sep}1{model_sep}pro{suffix_boundary}', s):
        return 'gemini-3.1-pro'
    if re.search(rf'(?<![a-z0-9])gemini{model_sep}3{model_sep}8{model_sep}flash{suffix_boundary}', s):
        return 'gemini-3.8-flash'
    if re.search(rf'(?<![a-z0-9])gemini{model_sep}3{model_sep}7{model_sep}flash{suffix_boundary}', s):
        return 'gemini-3.7-flash'
    if re.search(rf'(?<![a-z0-9])gemini{model_sep}3{model_sep}6{model_sep}flash{suffix_boundary}', s):
        return 'gemini-3.6-flash'
    gpt_suffix_boundary = r'(?![-._\s]+[a-z0-9])(?!\s*\(\s*(?!(?:high|medium)\s*\))[a-z])'
    if re.search(rf'(?<![a-z0-9])gpt{model_sep}oss{model_sep}120{model_sep}b{gpt_suffix_boundary}', s):
        return 'gpt-oss-120b'
    # 例如旧版 Claude、Gemini 3 Flash Preview 及其它变体都必须
    # 保留为 unknown，不能静默继承某个相近模型的单价。
    return None

def locate_antigravity_dir():
    """跨平台智能探测 Antigravity 本地数据根目录"""
    candidates = [
        os.path.expanduser('~/.gemini/antigravity-ide'),
        os.path.expanduser('~/.config/antigravity-ide'),
    ]
    if sys.platform == 'win32':
        appdata = os.environ.get('APPDATA', '')
        localappdata = os.environ.get('LOCALAPPDATA', '')
        userprofile = os.environ.get('USERPROFILE', '')
        if appdata:
            candidates.append(os.path.join(appdata, 'antigravity-ide'))
        if localappdata:
            candidates.append(os.path.join(localappdata, 'antigravity-ide'))
        if userprofile:
            candidates.append(os.path.join(userprofile, '.gemini', 'antigravity-ide'))
    elif sys.platform == 'darwin':
        candidates.append(os.path.expanduser('~/Library/Application Support/antigravity-ide'))

    for p in candidates:
        if os.path.exists(p) and (os.path.isdir(os.path.join(p, 'brain')) or os.path.isdir(os.path.join(p, 'conversations'))):
            return p
    return candidates[0]

import math

ESTIMATOR_VERSION = 'ab-char-v1'

def estimate_tokens(text):
    """字符级启发式分词估算器，与前端 workbench.html 保持 1.25 / 0.8 / 0.28 统一权重与 UTF-16 舍入同构"""
    if not text:
        return 0
    text_str = str(text)
    if not text_str.strip():
        return 0
    cjk_count = 0
    symbol_count = 0
    for ch in text_str:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FA5) or (0x3040 <= cp <= 0x30FF) or (0xAC00 <= cp <= 0xD7AF):
            cjk_count += 1
        elif ch in TOKEN_SYMBOLS:
            symbol_count += 1

    utf16_units = len(text_str.encode('utf-16-le')) // 2
    other_count = max(0, utf16_units - cjk_count - symbol_count)
    weighted = cjk_count * 1.25 + symbol_count * 0.8 + other_count * 0.28
    tokens = math.floor(weighted + 0.5)
    return max(1, tokens)

def format_tokens(val):
    """格式化展示 Token 数量"""
    if val < 1000:
        return f"{int(val):,}"
    if val < 1000000:
        return f"{val/1000:.2f}K"
    if val < 1000000000:
        return f"{val/1000000:.2f}M"
    return f"{val/1000000000:.2f}B"

def extract_conv_models_from_db(db_path, scan_stats=None):
    """从会话 SQLite DB 中的 gen_metadata 提取各轮调用的真实模型"""
    step_models = {}
    ambiguous_steps = set()
    if not os.path.exists(db_path):
        return step_models
    conn = None
    try:
        uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        c = conn.cursor()
        c.execute("SELECT data FROM gen_metadata ORDER BY idx;")
        for (data,) in c.fetchall():
            if not data:
                continue
            # 除机器 ID 外，部分 protobuf/blob 会保存 UI 展示名（含空格）。
            # SQLite BLOB 通常返回 bytes，但兼容 text/NULL，避免一行异常
            # 让整个数据库的模型映射被静默丢弃。
            if isinstance(data, (bytes, bytearray, memoryview)):
                blob_bytes = bytes(data)
            else:
                blob_bytes = str(data).encode('utf-8', errors='ignore')
            blob_text = blob_bytes.decode('latin1', errors='ignore').lower()
            found = []
            for marker in (
                'claude-opus-4.6', 'claude opus 4.6',
                'claude-sonnet-4.6', 'claude sonnet 4.6',
                'gpt-oss-120b', 'gpt oss 120b',
            ):
                if marker in blob_text:
                    found.append(marker.encode('latin1'))
            found.extend(re.findall(rb'(?:gemini-[0-9a-zA-Z.-]+|claude-[0-9a-zA-Z.-]+|gpt-[0-9a-zA-Z.-]+)', blob_bytes))
            candidates = []
            for f in found:
                s = f.decode('latin1')
                if s not in ['gemini-flash-aggre', 'gemini-flash-aggregate']:
                    norm_id = normalize_model_id(s)
                    if norm_id is not None and norm_id not in candidates:
                        candidates.append(norm_id)

            # SQLite blob 常保存展示名，例如 "Gemini 3.1 Pro (High)"，而不
            # 是连字符模型 ID。逐个扫描明确支持的 canonical ID，允许空格、
            # 点号和连字符作为分隔，但不接受泛化的 flash/pro/claude 猜测。
            for known_id in MODEL_PRICING_REGISTRY:
                parts = known_id.split('-')
                pattern = r'(?<![a-z0-9])' + r'[-._ ]*'.join(re.escape(p) for p in parts) + r'(?![-._\s]+[a-z0-9])(?!\s*\(\s*(?!high\s*\))[a-z])'
                if re.search(pattern, blob_text, re.IGNORECASE) and known_id not in candidates:
                    candidates.append(known_id)

            # 同一 metadata 行若包含多个候选（例如模型选择菜单和实际
            # 请求同时被序列化），无法证明哪一个用于该轮，宁可 unknown。
            if len(candidates) == 1:
                m = re.search(rb'last_step_index\x12([\x01-\x7f])([0-9]+)', blob_bytes)
                target_step = None
                if m:
                    try:
                        length = m.group(1)[0]
                        digits = m.group(2)[:length].decode('ascii')
                        # last_step_index 指向生成请求前的最后一个输入/工具
                        # step；紧随其后的 PLANNER_RESPONSE 才是该 metadata
                        # 对应的模型调用。
                        target_step = int(digits) + 1
                    except (IndexError, UnicodeDecodeError, ValueError):
                        target_step = None
                # gen_metadata 的内部行号不保证等于 transcript step_index；
                # 没有可解析的 last_step_index 时不能强行绑定。
                if target_step is not None and target_step not in ambiguous_steps:
                    previous = step_models.get(target_step)
                    if previous is not None and previous != candidates[0]:
                        step_models.pop(target_step, None)
                        ambiguous_steps.add(target_step)
                    else:
                        step_models[target_step] = candidates[0]
    except Exception as exc:
        if scan_stats is not None:
            scan_stats['db_metadata_errors'] += 1
        print(f"[!] 读取模型元数据失败，已跳过 {db_path}: {exc}", file=sys.stderr)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return step_models


def resolve_planner_models(planner_steps, db_step_models):
    """为 transcript 的 planner steps 建立可证明的模型映射。

    1. 优先通过 SQLite gen_metadata 与 transcript 的 step_index 精准对齐；
    2. 单一模型会话兜底：当会话内所有已知轮次均为同一种模型时，未匹配步骤（如 503 报错/重试）
       按该唯一主导模型兜底；
    3. 多模型混合会话严谨处理：若会话内观测到多种不同模型，未匹配步骤严格标记为 unknown，
       坚决不做跨模型邻近插值，避免将高价模型费率误套给低价模型。
    """
    resolved = {}
    for ordinal, step in enumerate(planner_steps):
        step_index = step.get('step_index')
        model_id = None
        if step_index is not None:
            model_id = db_step_models.get(step_index)
            if model_id is None:
                try:
                    model_id = db_step_models.get(int(step_index))
                except (TypeError, ValueError):
                    pass
        if model_id is not None:
            resolved[ordinal] = (model_id, 'sqlite_step_index')

    distinct = set(db_step_models.values())
    if len(distinct) == 1:
        # 当会话内已知轮次均为同一种模型时，未匹配步骤直接继承该主导模型
        model_id = next(iter(distinct))
        for ordinal in range(len(planner_steps)):
            resolved.setdefault(ordinal, (model_id, 'session_dominant_model'))
    elif len(distinct) > 1:
        # 多模型混合会话：未匹配步骤严格保留为 unknown，不作序号邻近插值
        for ordinal in range(len(planner_steps)):
            resolved.setdefault(ordinal, (None, 'unknown'))
    elif not resolved and not distinct:
        for ordinal in range(len(planner_steps)):
            resolved[ordinal] = (None, 'unknown')
    return resolved


def order_transcript_steps(steps):
    """按事件发生时间恢复 transcript 顺序，处理异步工具结果乱序写入。

    transcript.jsonl 的物理行顺序可能把工具输出写在发起它的
    PLANNER_RESPONSE 前面；created_at 才是主要时序，step_index 只作为同秒
    事件的次序。保留原始行号作为最后的稳定排序键，兼容分支/重试产生的
    重复 step_index。
    """
    def sort_key(item):
        original_index, step = item
        try:
            created_at = step.get('created_at', '')
            dt = datetime.fromisoformat(str(created_at).replace('Z', '+00:00'))
            time_key = dt.timestamp()
        except Exception:
            time_key = float('inf')
        try:
            step_key = int(step.get('step_index'))
        except (TypeError, ValueError):
            step_key = 10**12
        return time_key, step_key, original_index

    return [step for _, step in sorted(enumerate(steps), key=sort_key)]


def get_model_config(model_id):
    """返回已知模型配置；未知模型必须显式保留为 unknown。"""
    return MODEL_PRICING_REGISTRY.get(model_id)


def conversation_id_from_path(fpath):
    """从标准目录或归档路径提取会话 ID。

    标准日志位于 ``<id>/.system_generated/logs/transcript.jsonl``，但
    ``-a`` 也允许扫描诸如 ``archive/<id>/transcript.jsonl`` 的扁平归档。
    不能用固定的 ``parts[-4]``，否则所有扁平归档都会被错误合并成同一个
    会话。优先使用 UUID 目录名，最后才退回 transcript 的父目录名。
    """
    normalized = os.path.normpath(os.path.abspath(fpath))
    parts = normalized.split(os.sep)
    for p_idx in range(len(parts) - 1, -1, -1):
        if parts[p_idx] == '.system_generated' and p_idx > 0:
            return parts[p_idx - 1]

    uuid_pattern = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
        r'[89ab][0-9a-f]{3}-[0-9a-f]{12}$', re.IGNORECASE
    )
    for part in reversed(parts[:-1]):
        if uuid_pattern.fullmatch(part):
            return part

    parent = os.path.basename(os.path.dirname(normalized))
    return parent or 'unknown'

def scan_all_history(base_dir, include_archived=False, return_stats=False):
    """扫描所有会话的 transcript.jsonl 与关联 SQLite DB"""
    brain_dir = os.path.join(base_dir, 'brain')
    conv_db_dir = os.path.join(base_dir, 'conversations')
    standard_pattern = os.path.join(brain_dir, '*', '.system_generated', 'logs', 'transcript.jsonl')
    found_files = set(glob.glob(standard_pattern))
    archived_count = 0

    if include_archived:
        archive_patterns = [
            os.path.join(brain_dir, '**', 'transcript.jsonl'),
            os.path.join(base_dir, 'archive', '**', 'transcript.jsonl'),
            os.path.join(base_dir, 'archived', '**', 'transcript.jsonl'),
            os.path.join(base_dir, 'archived_brain', '**', 'transcript.jsonl'),
        ]
        for pat in archive_patterns:
            for fp in glob.glob(pat, recursive=True):
                norm_p = os.path.abspath(fp)
                if norm_p not in found_files:
                    found_files.add(norm_p)
                    archived_count += 1

    files = sorted(list(found_files))

    # 根据 conv_id 去重，防止原目录和备份目录的重复计算
    unique_files = {}
    for fpath in files:
        fpath = os.path.abspath(fpath)
        conv_id = conversation_id_from_path(fpath)
        # 同一个会话，优先取 brain 原目录而不是 archive
        if conv_id not in unique_files or ('archive' not in fpath and 'brain' in fpath):
            unique_files[conv_id] = fpath

    records = []
    model_stats = Counter()
    scan_stats = Counter()

    if include_archived and archived_count > 0:
        print(f"[*] 找到历史会话日志: 共 {len(unique_files)} 个不重复会话 (排除重复副本)")
    else:
        print(f"[*] 找到历史会话日志: 共 {len(unique_files)} 个会话目录{' (全量探测已开启)' if include_archived else ' (提示: 可添加 -a 参数扫描多级归档)'}")

    for conv_id, fpath in unique_files.items():

        # 读取关联 SQLite 数据库以提取真实模型映射
        db_path = os.path.join(conv_db_dir, f"{conv_id}.db")
        db_step_models = extract_conv_models_from_db(db_path, scan_stats)

        # 优先读取全量未截断日志 (transcript_full.jsonl)，不存在时退回精简版 (transcript.jsonl)
        full_fpath = fpath.replace('transcript.jsonl', 'transcript_full.jsonl')
        actual_fpath = full_fpath if os.path.exists(full_fpath) else fpath
        try:
            with open(actual_fpath, 'r', encoding='utf-8') as f:
                steps = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                        if not isinstance(parsed, dict):
                            scan_stats['non_object_json_lines'] += 1
                            continue
                        steps.append(parsed)
                    except json.JSONDecodeError:
                        scan_stats['invalid_json_lines'] += 1
                        continue
        except Exception as exc:
            scan_stats['skipped_files'] += 1
            print(f"[!] 读取 transcript 失败，已跳过 {fpath}: {exc}", file=sys.stderr)
            continue

        steps = order_transcript_steps(steps)
        planner_steps = [s for s in steps if s.get('type', '') == 'PLANNER_RESPONSE']
        planner_models = resolve_planner_models(planner_steps, db_step_models)

        current_input_text = ""
        current_date_str = ""
        current_hour_str = ""
        current_timestamp = 0
        planner_ordinal = 0
        last_valid_dt = None
        S_BASE = 0  # 离线回溯统一从 0 开始，与前端 DOM 起点保持一致
        CONTEXT_MAX = 1_000_000
        active_context_tokens = S_BASE
        prev_turn_total = 0
        for step in steps:

            step_type = step.get('type', '')
            created_at = step.get('created_at', '')

            local_dt = None
            if created_at:
                try:
                    clean_dt = created_at.replace('Z', '+00:00')
                    dt = datetime.fromisoformat(clean_dt)
                    local_dt = dt.astimezone()
                except Exception:
                    scan_stats['invalid_timestamp_values'] += 1

            if local_dt is None:
                # 严谨回退策略：优先继承本会话前序有效步骤时间，其次使用文件修改时间，严禁使用运行时 now() 污染当天统计
                if last_valid_dt is not None:
                    local_dt = last_valid_dt
                else:
                    try:
                        mtime = os.path.getmtime(fpath)
                        local_dt = datetime.fromtimestamp(mtime).astimezone()
                    except Exception:
                        local_dt = datetime(1970, 1, 1, 0, 0, 0)

            last_valid_dt = local_dt
            date_str = local_dt.strftime('%Y-%m-%d')
            hour_str = local_dt.strftime('%H:%M')
            timestamp = int(local_dt.timestamp() * 1000)

            # 严格仅判定真正的大模型生成步 (PLANNER_RESPONSE)，排除工具执行结果 (RUN_COMMAND, VIEW_FILE 等)
            # 1. 遇到脑图压缩检查点 (Compaction Checkpoint)，发生隐性上下文折叠重置
            if step_type == 'CHECKPOINT':
                summary_tok = estimate_tokens(str(step.get('content', '') or ''))
                active_context_tokens = S_BASE + summary_tok
                prev_turn_total = active_context_tokens
                continue

            # 2. 严格判定大模型生成步 (PLANNER_RESPONSE)
            if step_type == 'PLANNER_RESPONSE':
                output_content = str(step.get('content', '') or '')
                thinking_content = str(step.get('thinking', '') or '')
                tool_calls = step.get('tool_calls', [])
                tool_str = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else ''
                combined_output = output_content + ("\n" + thinking_content if thinking_content else "") + ("\n" + tool_str if tool_str else "")
                out_tok = estimate_tokens(combined_output)
                in_tok = active_context_tokens  # 物理在席输入上下文

                # 前缀缓存计算 (以 2000 为冷启动门槛，以 72% 为动态上限拟合平稳缓存命中率)
                if prev_turn_total >= 2000:
                    cache_tok = min(prev_turn_total, int(in_tok * 0.72))
                    new_input_tok = max(0, in_tok - cache_tok)
                else:
                    cache_tok = 0
                    new_input_tok = in_tok
                total_tok = in_tok + out_tok

                # 空的/取消的 planner 事件没有可从 transcript 恢复的
                # token；跳过它，令离线导出轮次与前端导入轮次保持一致。
                # 仍递增 ordinal，避免后续 SQLite 模型绑定错位。
                if total_tok <= 0:
                    planner_ordinal += 1
                    current_input_text = ""
                    current_date_str = ""
                    current_hour_str = ""
                    current_timestamp = 0
                    continue

                model_info = planner_models.get(planner_ordinal)
                planner_ordinal += 1
                model_id, model_provenance = model_info if model_info else (None, 'unknown')
                model_cfg = get_model_config(model_id)

                # 阶梯计价逻辑 (> 200k 对于 3.1 Pro)
                if model_cfg is not None:
                    is_200k = in_tok > 200000
                    p_input = model_cfg.get('input_200k', model_cfg['input']) if is_200k else model_cfg['input']
                    p_output = model_cfg.get('output_200k', model_cfg['output']) if is_200k else model_cfg['output']
                    p_cache = model_cfg.get('cache_200k', model_cfg['cache']) if is_200k else model_cfg.get('cacheRead', model_cfg.get('cache', 0))
                    cost = (new_input_tok * p_input + out_tok * p_output + cache_tok * p_cache) / 1_000_000.0
                    model_name = model_cfg['name']
                    family = model_cfg['family']
                    model_stats[model_name] += 1
                    cost_provenance = 'estimated'
                else:
                    cost = None
                    model_name = '未知模型'
                    family = 'unknown'
                    model_stats[model_name] += 1
                    cost_provenance = 'unknown'

                # step_index 在不同 transcript 类型之间会重复，不能作为唯一
                # ID。把 planner ordinal 放入 ID，避免导入/审计时出现重复主键。
                planner_id = planner_ordinal - 1
                step_index = step.get('step_index')
                step_suffix = str(planner_id) if step_index is None else f"{planner_id}_{step_index}"
                rec_id = f"hist_{conv_id}_{step_suffix}"
                records.append({
                    'id': rec_id,
                    'convId': conv_id,
                    'timestamp': current_timestamp or timestamp,
                    'dateStr': current_date_str or date_str,
                    'hourStr': current_hour_str or hour_str,
                    'modelId': model_id or 'unknown',
                    'modelName': model_name,
                    'family': family,
                    'inTokens': in_tok,
                    'outTokens': out_tok,
                    'inputTokens': in_tok,
                    'outputTokens': out_tok,
                    'cacheTokens': cache_tok,
                    'totalTokens': total_tok,
                    # 不要在逐轮阶段按 6 位小数截断：大量短轮次的费用
                    # 会被直接舍为 0，累计时形成可见漏算。保留 9 位，
                    # 最终汇总/界面再按展示精度四舍五入。
                    'costUsd': round(cost, 9) if cost is not None else None,
                    'source': 'offline-estimate',
                    'estimated': True,
                    'cacheKnown': True,
                    # 即使模型单价已知，transcript 也没有 provider 的真实
                    # prompt/output/cache usage；该金额只是“无缓存假设下”的
                    # 文本估算，不能在前端显示成完整费用。
                    'costKnown': True if model_id != 'unknown' else False,
                    'provenance': {
                        'model': model_provenance,
                        'inputTokens': 'estimated',
                        'outputTokens': 'estimated',
                        'cacheTokens': 'estimated',
                        'cacheHitRate': f'{(cache_tok / total_tok * 100) if total_tok > 0 else 0:.2f}%',
                        'totalTokens': 'estimated',
                        'costUsd': cost_provenance
                    }
                })

                active_context_tokens = min(CONTEXT_MAX, in_tok + out_tok)
                prev_turn_total = active_context_tokens
                current_date_str = ""
                current_hour_str = ""
                current_timestamp = 0
            else:
                step_tok = estimate_tokens(str(step.get('content', '') or ''))
                active_context_tokens += step_tok
                # 动态物理上下文上限封顶 (最大 1M tokens)
                active_context_tokens = min(CONTEXT_MAX, active_context_tokens)
                if not current_date_str:
                    current_date_str = date_str
                    current_hour_str = hour_str
                    current_timestamp = timestamp

    if scan_stats:
        print(f"[!] 扫描警告统计: {dict(scan_stats)}", file=sys.stderr)
    if return_stats:
        return records, model_stats, dict(scan_stats)
    return records, model_stats

def aggregate_records(records):
    daily = {}
    total_tokens = 0
    total_cache = 0
    total_cost = 0.0

    for r in records:
        d = r.get('dateStr') or 'unknown-date'
        if d not in daily:
            daily[d] = {'tokens': 0, 'cache': 0, 'cost': 0.0, 'turns': 0}
        daily[d]['tokens'] += r.get('totalTokens') or 0
        daily[d]['cache'] += r.get('cacheTokens') or 0
        daily[d]['cost'] += r.get('costUsd') or 0.0
        daily[d]['turns'] += 1

        total_tokens += r.get('totalTokens') or 0
        total_cache += r.get('cacheTokens') or 0
        total_cost += r.get('costUsd') or 0.0

    return daily, total_tokens, total_cache, total_cost

def main():
    parser = argparse.ArgumentParser(
        description="Antigravity 历史全量会话深度扫描与 Token 计费导出脚本 (跨平台全功能版)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python scan_history_conversations.py                 # 标准扫描所有历史会话
  python scan_history_conversations.py -a              # 全量深度扫描，递归包含所有归档与备份会话
  python scan_history_conversations.py -a -o my.json   # 扫描并导出到自定义文件
        """
    )
    parser.add_argument('-a', '--all', '--include-archived', action='store_true', dest='include_archived',
                        help='全量深度扫描，包含归档目录（如 archive/、archived_brain/ 等）')
    parser.add_argument('-d', '--dir', type=str, default=None,
                        help='手动指定 Antigravity 数据根目录路径')
    parser.add_argument('-o', '--output', type=str, default=None,
                        help='指定输出 JSON 文件路径（默认当前目录下 antigravity_history_tokens.json）')
    args = parser.parse_args()

    print("=" * 70)
    print(" Antigravity 历史全量会话深度扫描与 Token 计费导出脚本 (v0.3.0)")
    print("=" * 70)

    base_dir = args.dir if args.dir else locate_antigravity_dir()
    print(f"[*] 探测到 Antigravity 数据根目录: {base_dir}")
    if args.include_archived:
        print("[*] 扫描模式: 全量扫描 (包含归档与多级备份目录, -a)")
    else:
        print("[*] 扫描模式: 标准扫描 (提示: 可使用 -a / --all 包含归档会话)")

    if not os.path.exists(base_dir):
        print(f"[!] 未找到存储目录: {base_dir}")
        print("[!] 请确保当前系统已安装并运行过 Antigravity IDE。")
        sys.exit(1)

    records, model_stats, scan_stats = scan_all_history(
        base_dir,
        include_archived=args.include_archived,
        return_stats=True
    )
    if not records:
        print("[!] 未扫描到任何有效会话轮次记录。")
        sys.exit(0)

    daily, total_tokens, total_cache, total_cost = aggregate_records(records)

    print("\n" + "-" * 70)
    print(" [📊 模型识别与使用分布统计]")
    print("-" * 78)
    for m_name, cnt in model_stats.most_common():
        pct = (cnt / len(records)) * 100
        print(f"  ● {m_name:<20}: {cnt:>6} 轮对话 ({pct:>5.1f}%)")

    print("\n" + "-" * 78)
    print(f" {'日期 (Date)':<12} | {'总 Tokens':<12} | {'缓存 Tokens':<12} | {'命中率':<8} | {'预估费用 ($)':<12} | {'轮次'}")
    print("-" * 78)

    for d in sorted(daily.keys(), reverse=True)[:15]:
        item = daily[d]
        # cache=0 只是兼容旧导入 schema，不代表 provider 报告了零缓存。
        rate_str = f"{(item['cache']/item['tokens']*100):.2f}%" if item['tokens'] > 0 else "0.00%"
        print(f" {d:<12} | {format_tokens(item['tokens']):<12} | {format_tokens(item['cache']):<12} | {rate_str:<8} | ${item['cost']:<11.4f} | {item['turns']}")

    if len(daily) > 15:
        print(f" ... 及其余 {len(daily) - 15} 天的历史记录")

    print("-" * 78)
    cum_cache_rate = (total_cache / total_tokens * 100) if total_tokens > 0 else 0
    print(f" 总计概览: {len(records)} 轮会话 | 总计 {format_tokens(total_tokens)} Tokens | 缓存命中 {format_tokens(total_cache)} ({cum_cache_rate:.2f}%) | 预估总支出(已知模型): ${total_cost:.4f}")
    print("-" * 78)

    # 确保全量记录按时间戳严格升序排序
    records.sort(key=lambda x: x['timestamp'])

    # 导出包含全量模型与时间戳的 JSON 文件
    if args.output:
        out_path = os.path.abspath(args.output)
    else:
        out_dir = os.path.abspath(os.getcwd())
        out_path = os.path.join(out_dir, "antigravity_history_tokens.json")

    known_cost_records = sum(1 for r in records if r.get('provenance', {}).get('costUsd') == 'estimated')
    unknown_model_records = sum(1 for r in records if r.get('modelId') == 'unknown')
    export_payload = {
        'version': '0.3.0',
        'estimatorVersion': ESTIMATOR_VERSION,
        'generatedAt': datetime.now().isoformat(),
        'provenance': {
            'inputTokens': 'estimated_from_transcript_text',
            'inputScope': 'text_between_planner_events_not_full_provider_prompt',
            'outputTokens': 'estimated_from_transcript_text',
            'cacheTokens': 'estimated_from_session_context',
            'costUsd': 'estimated_for_known_models_only',
            'model': 'sqlite_step_index_or_single_turn_metadata_when_bound_or_unknown'
        },
        'scanSummary': {
            'totalRecords': len(records),
            'totalTokens': total_tokens,
            'totalCacheTokens': total_cache,
            'totalCostUsd': round(total_cost, 4),
            'costRecordsEstimated': known_cost_records,
            'unknownModelRecords': unknown_model_records,
            'totalCacheTokens': total_cache,
            'cumCacheRate': f"{(total_cache / total_tokens * 100) if total_tokens > 0 else 0:.2f}%",
            'cacheTokensKnown': True,
            'billingReady': True,
            'scanWarnings': scan_stats
        },
        'records': records
    }

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(export_payload, f, ensure_ascii=False, indent=2)

    print("\n[✓] 扫描成功！已生成全量历史数据 JSON 文件:")
    print(f"    👉 导出的绝对文件路径: {out_path}")
    print("\n[📖 导入指引]:")
    print("    1. 打开 Antigravity 界面，点击右上角「📊 用量」浮窗")
    print("    2. 直接点击顶部标题栏的「📥 导入」按钮（或全屏大屏「⚙️ 数据与维护」页面的导入按钮）")
    print(f"    3. 选择上述导出的文件 ({out_path})")
    print("    ⚡ 导入将直接以历史记录为基准，自动同步刷新折线图与用量账单！")
    print("    ⚠️  [说明]: 本算法费用为本地估算拟合，可能与官方实际账单存在差异，仅供开发参考。\n")

if __name__ == '__main__':
    main()
