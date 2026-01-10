import argparse
import collections
import json
import logging
import os
import subprocess
import sys
import time
import ast
import builtins

from .python import Python
from .model import (TRUNK_COLOR, LEAF_COLOR, NODE_COLOR, GROUP_TYPE, OWNER_CONST,
                    Edge, Group, Node, Variable, is_installed, flatten, Call, djoin)

# Global switch to enable/disable heuristic resolution features.
# Controlled by CLI flag --heuristics / --no-heuristics (default: enabled).
_HEURISTICS_ENABLED = True

def resolve_import_path(module_name, base_dir):
    """Resolve a dotted module name to a local .py file under base_dir.

    Example: 'exclude_modules_b' -> '<base_dir>/exclude_modules_b.py'
    """
    parts = module_name.split('.')
    candidate = os.path.join(base_dir, *parts) + '.py'
    if os.path.exists(candidate):
        return candidate
    return None


def parse_file_recursive(file_path, base_dir, parsed_files, language_ext):
    """Parse a file and recursively parse local imports, returning a list of Groups.

    Keeps track of parsed files in `parsed_files` to avoid cycles.
    """
    if file_path in parsed_files:
        return []
    parsed_files.add(file_path)

    with open(file_path, encoding='utf-8') as f:
        tree = ast.parse(f.read())

    # Create the file group for this file
    file_group = make_file_group(tree, file_path, language_ext)
    groups = [file_group]

    # Walk AST for import statements and recurse for local modules
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dep_path = resolve_import_path(alias.name, base_dir)
                if dep_path:
                    groups += parse_file_recursive(dep_path, base_dir, parsed_files, language_ext)
        elif isinstance(node, ast.ImportFrom):
            # Handle ImportFrom in several cases:
            # 1) node.module present and resolveable by dotted name
            # 2) node.module present but not resolveable: try each alias as a local module
            # 3) node.module is None (relative import like `from . import name`): try alias in base_dir
            if node.module:
                dep_path = resolve_import_path(node.module, base_dir)
                if dep_path:
                    groups += parse_file_recursive(dep_path, base_dir, parsed_files, language_ext)
                else:
                    # module couldn't be resolved as dotted path; try alias names as local modules
                    for alias in node.names:
                        candidate = resolve_import_path(alias.name, base_dir)
                        if candidate:
                            groups += parse_file_recursive(candidate, base_dir, parsed_files, language_ext)
            else:
                # Relative import or `from . import name`.
                for alias in node.names:
                    candidate = os.path.join(base_dir, alias.name) + '.py'
                    if os.path.exists(candidate):
                        groups += parse_file_recursive(candidate, base_dir, parsed_files, language_ext)
                        continue
                    # Support explicit relative levels if present (node.level)
                    level = getattr(node, 'level', 0) or 0
                    if level:
                        cur = base_dir
                        for _ in range(level):
                            cur = os.path.dirname(cur)
                        parent_candidate = os.path.join(cur, alias.name) + '.py'
                        if os.path.exists(parent_candidate):
                            groups += parse_file_recursive(parent_candidate, base_dir, parsed_files, language_ext)

    return groups

VERSION = '2.5.1'

IMAGE_EXTENSIONS = ('png', 'svg')
TEXT_EXTENSIONS = ('dot', 'gv', 'json')
VALID_EXTENSIONS = IMAGE_EXTENSIONS + TEXT_EXTENSIONS

DESCRIPTION = "Generate flow charts from your source code. " \
              "See the README at https://github.com/scottrogowski/code2flow."


LEGEND = """subgraph legend{
    rank = min;
    label = "legend";
    Legend [shape=none, margin=0, label = <
        <table cellspacing="0" cellpadding="0" border="1"><tr><td>Code2flow Legend</td></tr><tr><td>
        <table cellspacing="0">
        <tr><td>Regular function</td><td width="50px" bgcolor='%s'></td></tr>
        <tr><td>Trunk function (nothing calls this)</td><td bgcolor='%s'></td></tr>
        <tr><td>Leaf function (this calls nothing else)</td><td bgcolor='%s'></td></tr>
        <tr><td>Function call</td><td><font color='black'>&#8594;</font></td></tr>
        </table></td></tr></table>
        >];
}""" % (NODE_COLOR, TRUNK_COLOR, LEAF_COLOR)


LANGUAGES = {
    'py': Python,
}


class LanguageParams():
    """
    Shallow structure to make storing language-specific parameters cleaner
    """
    def __init__(self, source_type='script', ruby_version='27'):
        self.source_type = source_type
        self.ruby_version = ruby_version


class SubsetParams():
    """
    Shallow structure to make storing subset-specific parameters cleaner.
    """
    def __init__(self, target_function, upstream_depth, downstream_depth):
        self.target_function = target_function
        self.upstream_depth = upstream_depth
        self.downstream_depth = downstream_depth

    @staticmethod
    def generate(target_function, upstream_depth, downstream_depth):
        """
        :param target_function str:
        :param upstream_depth int:
        :param downstream_depth int:
        :rtype: SubsetParams|Nonetype
        """
        if upstream_depth and not target_function:
            raise AssertionError("--upstream-depth requires --target-function")

        if downstream_depth and not target_function:
            raise AssertionError("--downstream-depth requires --target-function")

        if not target_function:
            return None

        if not (upstream_depth or downstream_depth):
            raise AssertionError("--target-function requires --upstream-depth or --downstream-depth")

        if upstream_depth < 0:
            raise AssertionError("--upstream-depth must be >= 0. Exclude argument for complete depth.")

        if downstream_depth < 0:
            raise AssertionError("--downstream-depth must be >= 0. Exclude argument for complete depth.")

        return SubsetParams(target_function, upstream_depth, downstream_depth)



def _find_target_node(subset_params, all_nodes):
    """
    Find the node referenced by subset_params.target_function
    :param subset_params SubsetParams:
    :param all_nodes list[Node]:
    :rtype: Node
    """
    target_nodes = []
    for node in all_nodes:
        if node.token == subset_params.target_function or \
           node.token_with_ownership() == subset_params.target_function or \
           node.name() == subset_params.target_function:
            target_nodes.append(node)
    if not target_nodes:
        raise AssertionError("Could not find node %r to build a subset." % subset_params.target_function)
    if len(target_nodes) > 1:
        raise AssertionError("Found multiple nodes for %r: %r. Try either a `class.func` or "
                             "`filename::class.func`." % (subset_params.target_function, target_nodes))
    return target_nodes[0]


def _filter_nodes_for_subset(subset_params, all_nodes, edges):
    """
    Given subset_params, return a set of all nodes upstream and downstream of the target node.
    :param subset_params SubsetParams:
    :param all_nodes list[Node]:
    :param edges list[Edge]:
    :rtype: set[Node]
    """
    target_node = _find_target_node(subset_params, all_nodes)
    downstream_dict = collections.defaultdict(set)
    upstream_dict = collections.defaultdict(set)
    for edge in edges:
        upstream_dict[edge.node1].add(edge.node0)
        downstream_dict[edge.node0].add(edge.node1)

    include_nodes = {target_node}
    step_nodes = {target_node}
    next_step_nodes = set()

    for _ in range(subset_params.downstream_depth):
        for node in step_nodes:
            next_step_nodes.update(downstream_dict[node])
        include_nodes.update(next_step_nodes)
        step_nodes = next_step_nodes
        next_step_nodes = set()

    step_nodes = {target_node}
    next_step_nodes = set()

    for _ in range(subset_params.upstream_depth):
        for node in step_nodes:
            next_step_nodes.update(upstream_dict[node])
        include_nodes.update(next_step_nodes)
        step_nodes = next_step_nodes
        next_step_nodes = set()

    return include_nodes


def _filter_edges_for_subset(new_nodes, edges):
    """
    Given the subset of nodes, filter for edges within this subset
    :param new_nodes set[Node]:
    :param edges list[Edge]:
    :rtype: list[Edge]
    """
    new_edges = []
    for edge in edges:
        if edge.node0 in new_nodes and edge.node1 in new_nodes:
            new_edges.append(edge)
    return new_edges


def _filter_groups_for_subset(new_nodes, file_groups):
    """
    Given the subset of nodes, do housekeeping and filter out for groups within this subset
    :param new_nodes set[Node]:
    :param file_groups list[Group]:
    :rtype: list[Group]
    """
    for file_group in file_groups:
        for node in file_group.all_nodes():
            if node not in new_nodes:
                node.remove_from_parent()

    new_file_groups = [g for g in file_groups if g.all_nodes()]

    for file_group in new_file_groups:
        for group in file_group.all_groups():
            if not group.all_nodes():
                group.remove_from_parent()

    return new_file_groups


def _filter_for_subset(subset_params, all_nodes, edges, file_groups):
    """
    Given subset_params, return the subset of nodes, edges, and groups
    upstream and downstream of the target node.
    :param subset_params SubsetParams:
    :param all_nodes list[Node]:
    :param edges list[Edge]:
    :param file_groups list[Group]:
    :rtype: list[Group], list[Node], list[Edge]
    """
    new_nodes = _filter_nodes_for_subset(subset_params, all_nodes, edges)
    new_edges = _filter_edges_for_subset(new_nodes, edges)
    new_file_groups = _filter_groups_for_subset(new_nodes, file_groups)
    return new_file_groups, list(new_nodes), new_edges


def generate_json(nodes, edges):
    '''
    Generate a json string from nodes and edges
    See https://github.com/jsongraph/json-graph-specification

    :param nodes list[Node]: functions
    :param edges list[Edge]: function calls
    :rtype: str
    '''
    nodes = [n.to_dict() for n in nodes]
    nodes = {n['uid']: n for n in nodes}
    edges = [e.to_dict() for e in edges]

    return json.dumps({"graph": {
        "directed": True,
        "nodes": nodes,
        "edges": edges,
    }})


def write_file(outfile, nodes, edges, groups, hide_legend=False,
               no_grouping=False, as_json=False):
    '''
    Write a dot file that can be read by graphviz

    :param outfile File:
    :param nodes list[Node]: functions
    :param edges list[Edge]: function calls
    :param groups list[Group]: classes and files
    :param hide_legend bool:
    :rtype: None
    '''

    if as_json:
        content = generate_json(nodes, edges)
        outfile.write(content)
        return

    splines = "polyline" if len(edges) >= 500 else "ortho"

    content = "digraph G {\n"
    content += "concentrate=true;\n"
    content += f'splines="{splines}";\n'
    content += 'rankdir="LR";\n'
    if not hide_legend:
        content += LEGEND
    for node in nodes:
        content += node.to_dot() + ';\n'
    for edge in edges:
        content += edge.to_dot() + ';\n'
    if not no_grouping:
        for group in groups:
            content += group.to_dot()
    content += '}\n'
    outfile.write(content)


def determine_language(individual_files):
    """
    Given a list of filepaths, determine the language from the first
    valid extension

    :param list[str] individual_files:
    :rtype: str
    """
    for source, _ in individual_files:
        suffix = source.rsplit('.', 1)[-1]
        if suffix in LANGUAGES:
            logging.info("Implicitly detected language as %r.", suffix)
            return suffix
    raise AssertionError(f"Language could not be detected from input {individual_files}. ",
                         "Try explicitly passing the language flag.")


def get_sources_and_language(raw_source_paths, language):
    """
    Given a list of files and directories, return just files.
    If we are not passed a language, determine it.
    Filter out files that are not of that language

    :param list[str] raw_source_paths: file or directory paths
    :param str|None language: Input language
    :rtype: (list, str)
    """

    individual_files = []
    for source in sorted(raw_source_paths):
        # raw_source_paths内のファイル/フォルダのツリーリストを作成する。
        if os.path.isfile(source):
            individual_files.append((source, True))
            # ToDo: individual_filesの要素のTrue/Falseはどういう意味？
            #  raw_source_paths直下にあるファイルのみTrue？
            #  そこを区別する必要性は何？
            continue
        for root, _, files in os.walk(source):
            # os.walkは引数のディレクトリ内を再帰的に走査して
            # 各ディレクトリ(top自身を含む)ごとに、タプル(dirpath, dirnames, filenames)をyieldする。
            # sourceがファイルの場合は上でcontinueされているので、ここを通るsourceはすべてディレクトリ。
            # https://docs.python.org/ja/3/library/os.html#os.walk
            for f in files:
                individual_files.append((os.path.join(root, f), False))

    if not individual_files:
        raise AssertionError("No source files found from %r" % raw_source_paths)
    logging.info("Found %d files from sources argument.", len(individual_files))

    if not language:
        language = determine_language(individual_files)
        # 起動時に言語が指定されていない場合はここで決定(推定)する。
        # ToDo: Expected type 'list[str]', got 'list[tuple[str, bool]]' instead
        #  という警告が出ている。それはそうなんだけどdetermine_languageではどう使われてんだろうか。

    sources = set()
    for source, explicity_added in individual_files:
        if explicity_added or source.endswith('.' + language):
            # raw_source_paths直下にあるファイルとターゲット言語のソースファイルをsourcesに加える。
            # ToDo: ここsetなの？ 別階層に同名のファイルあったらゴチャゴチャにならんかね。
            sources.add(source)
        else:
            logging.info("Skipping %r which is not a %s file. "
                         "If this is incorrect, include it explicitly.",
                         source, language)

    if not sources:
        raise AssertionError("Could not find any source files given {raw_source_paths} "
                             "and language {language}.")

    sources = sorted(list(sources))
    logging.info("Processing %d source file(s)." % (len(sources)))
    for source in sources:
        logging.info("  " + source)

    return sources, language


def make_file_group(tree: ast.AST, filename: str, extension: str) -> Group:
    """
    Given an AST for the entire file, generate a file group complete with
    subgroups, nodes, etc.

    :param tree ast.AST:
    :param filename str:
    :param extension str:

    :rtype: Group
    """
    language = LANGUAGES[extension]

    # ToDo: tree.body[4].body[1].targets[0].idにreqが格納されている。
    #   reqではなくrequest.getをnode_trees内にNodeとして登録したい。
    subgroup_trees, node_trees, body_trees, import_tree = language.separate_namespaces(tree)
    # tree内のast要素を分類してリストにして返す。

    group_type = GROUP_TYPE.FILE
    token = os.path.split(filename)[-1].rsplit('.' + extension, 1)[0]
    line_number = 0
    display_name = 'File'
    import_tokens = language.file_import_tokens(filename)

    file_group = Group(token, group_type, display_name, import_tokens,
                       line_number, parent=None)

    # Include import statements in the root node lines so module-level
    # imports are recorded into the module scope (used when resolving
    # names inside functions). Create the root node first so subsequent
    # function nodes can inherit the module scope.
    root_lines = list(body_trees) + list(import_tree)
    file_group.add_node(language.make_root_node(root_lines, parent=file_group), is_root=True)

    for node_tree in node_trees:
        # node_treeはast.FunctionDef or ast.AsyncFunctionDef
        for new_node in language.make_nodes(node_tree, parent=file_group):
            file_group.add_node(new_node)

    for subgroup_tree in subgroup_trees:
        file_group.add_subgroup(language.make_class_group(subgroup_tree, parent=file_group))

    # for import_module in import_tree:
    #     # ToDo: importモジュールを取り出してリストに詰めるが、これでいいのかというと？？？
    #     file_group.add_import(import_module.names[0])
    for import_module in import_tree:
        for new_node in language.make_import_module_nodes(import_module, parent=file_group):
            # file_group.add_import(new_node)
            file_group.add_node(new_node)
    # Todo: import_treeはast.Import（ast.FunctionDefとの構造の違いに注意）
    #  なのでこれをdot言語に起こせるように成形してNodeに詰める。
    #  dot言語作成時に読む属性はlabel, name, shape, style, fillcolor
    #  （詳細はmodel.py Node.to_dot()参照）
    #  add_node(new_node)では、new_node側にNodeの構造を持たせて、add_nodeはlistにappendしているだけ。
    #  Nodeの構造はPython.make_nodes()参照。

    return file_group


def _find_library_node_by_signature(call: Call, all_nodes: list[Node]) -> Node:
    """
    ライブラリ呼び出しに対応するライブラリノードを検索する
    
    :param call Call: ライブラリ呼び出し
    :param all_nodes list[Node]: すべてのノード
    :rtype: Node|None
    """
    if not (call.is_library and call.is_attr() and call.owner_token):
        return None
    
    lib_signature = djoin(call.owner_token, call.token)
    module_name = call.owner_token.split('.')[0] if '.' in call.owner_token else call.owner_token

    # Debug: list candidate nodes that share the token
    candidates = [n for n in all_nodes if n.token == call.token]
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        logging.debug(f"[_find_library_node_by_signature] lib_signature={lib_signature} module_name={module_name} candidates={[(n.token, getattr(n.parent,'token',None), n.import_tokens, n.is_library) for n in candidates]}")

    # Extra targeted debug for problematic calls
    if call.token == 'add_mutually_exclusive_group':
        logging.debug(f"[_find_library_node_by_signature] TARGET CALL: owner_token={call.owner_token} is_attr={call.is_attr()} is_library={call.is_library} lib_signature={lib_signature}")

    # Prefer concrete nodes (from parsed files) whose import_tokens match the signature
    # or whose parent module name matches. Do not restrict to node.is_library; real
    # file nodes should be considered first to avoid misattributing calls to libraries
    # when a local module defines the same function name.
    for node in candidates:
        if lib_signature in (node.import_tokens or []):
            logging.debug(f"[_find_library_node_by_signature] matched by import_tokens: {node.token} parent={getattr(node.parent,'token',None)}")
            return node

        if isinstance(node.parent, Group) and node.parent.token == module_name:
            logging.debug(f"[_find_library_node_by_signature] matched by parent token: {node.token} parent={node.parent.token}")
            return node

    # Fallback: if no concrete node matched, look for synthesized library nodes
    for node in all_nodes:
        if not (node.is_library and node.token == call.token):
            continue

        if lib_signature in (node.import_tokens or []):
            logging.debug(f"[_find_library_node_by_signature] matched synthesized by import_tokens: {node.token} parent={getattr(node.parent,'token',None)}")
            return node

        if isinstance(node.parent, Group) and node.parent.token == module_name:
            logging.debug(f"[_find_library_node_by_signature] matched synthesized by parent token: {node.token} parent={node.parent.token}")
            return node

    logging.debug(f"[_find_library_node_by_signature] no match found for {lib_signature}")
    return None


def _find_library_node_from_variable(call: Call, var: Variable, all_nodes: list[Node]) -> Node:
    """
    変数がライブラリインスタンスの場合、その変数経由のメソッド呼び出しに対応するライブラリノードを検索する
    
    :param call Call: メソッド呼び出し（例：parser.add_argument()）
    :param var Variable: 変数（例：parser = argparse.ArgumentParser()）
    :param all_nodes list[Node]: すべてのノード
    :rtype: Node|None
    """
    if not (call.is_attr() and call.owner_token == var.token):
        return None
    
    if not isinstance(var.points_to, Call):
        logging.debug(f"[_find_library_node_from_variable] var.points_toはCallではない: {type(var.points_to).__name__}")
        return None
    
    library_call = var.points_to
    if not (library_call.is_library and library_call.is_attr()):
        logging.debug(f"[_find_library_node_from_variable] var.points_toはライブラリ呼び出しではない: is_library={library_call.is_library}, is_attr()={library_call.is_attr()}")
        return None
    
    # 元のライブラリ呼び出し（例：argparse.ArgumentParser）からモジュール名を取得
    lib_module_name = library_call.owner_token.split('.')[0] if '.' in library_call.owner_token else library_call.owner_token
    logging.debug(f"[_find_library_node_from_variable] ライブラリモジュール名: {lib_module_name}, 検索するメソッド名: {call.token}")
    
    # 対応するライブラリノードを検索（メソッド名で検索、例：add_argument）
    candidates = [n for n in all_nodes if n.token == call.token]
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        logging.debug(f"[_find_library_node_from_variable] lib_module_name={lib_module_name} call.token={call.token} candidates={[(n.token, getattr(n.parent,'token',None), n.import_tokens, n.is_library) for n in candidates]}")

    for node in candidates:
        if node.is_library and node.token == call.token:
            if isinstance(node.parent, Group) and node.parent.token == lib_module_name:
                logging.debug(f"[_find_library_node_from_variable] ライブラリノードが見つかりました: {node.token} parent={node.parent.token}")
                return node

    # Targeted debug when lookup fails for specific method
    if call.token == 'add_mutually_exclusive_group':
        logging.debug(f"[_find_library_node_from_variable] NO MATCH for token={call.token}, var.token={var.token}, lib_module_name={lib_module_name}, candidates_tokens={[n.token for n in candidates]}")
    
    logging.debug(f"[_find_library_node_from_variable] ライブラリノードが見つかりませんでした: token={call.token}, module={lib_module_name}")
    return None


def _find_link_for_call(call: Call, node_a: Node, all_nodes: list[Node]):
    """
    Given a call that happened on a node (node_a), return the node
    that the call links to and the call itself if >1 node matched.
    ノード (node_a) で発生した呼び出しが指定されると、

    呼び出しがリンクされているノードと、一致するノードが 1 つ以上ある場合は呼び出し自体を返します。

    :param call Call:
    :param node_a Node:
    :param all_nodes list[Node]:

    :returns: The node it links to and the call if >1 node matched.
    :rtype: (Node|None, Call|None)
    """
    logging.debug(f"[_find_link_for_call] 呼び出しチェック開始: node_a.token={node_a.token}, call.to_string()={call.to_string()}, call.owner_token={call.owner_token}, call.token={call.token}, call.is_attr()={call.is_attr()}, call.is_library={call.is_library}")

    # Targeted debug for methods we care about
    if call.token == 'add_mutually_exclusive_group':
        logging.debug(f"[_find_link_for_call] TARGET: checking add_mutually_exclusive_group for node {node_a.token}. call.owner_token={call.owner_token} is_attr={call.is_attr()} is_library={call.is_library}")

    # Handle calls made via super(), e.g. `super().method()`.
    # When we see an attribute call whose owner_token is 'super', resolve
    # the call to the appropriate method on the first matching parent
    # class (if heuristics are enabled and inheritance info is available).
    # NOTE: This resolution only works when the parent class's methods are
    # represented in `parent_group.inherits` (i.e. the parent class was
    # parsed/available to the resolver). If the parent class is defined in
    # an external module that was not parsed (or could not be resolved),
    # no match will be found and no `via super` link will be created.
    if _HEURISTICS_ENABLED and call.is_attr() and call.owner_token == 'super':
        parent_group = getattr(node_a, 'parent', None)
        candidates = []
        if parent_group and getattr(parent_group, 'group_type', None) == GROUP_TYPE.CLASS:
            # Search through inherited node lists (each entry is a list of nodes)
            for inherit_nodes in getattr(parent_group, 'inherits', []):
                for candidate in inherit_nodes:
                    if candidate.token == call.token:
                        candidates.append(candidate)

            if candidates:
                if len(candidates) == 1:
                    # Mark the call so the edge can be labeled later
                    try:
                        call.via_super = True
                    except Exception:
                        pass
                    logging.debug(f"[_find_link_for_call] super() によって継承側メソッドをマッチ: {candidates[0].token} parent={getattr(candidates[0].parent,'token',None)}")
                    return candidates[0], None, None
                else:
                    logging.debug(f"[_find_link_for_call] super() によって複数候補: {[c.token for c in candidates]}")
                    return None, call, (candidates, [])
    # 0.5. owner == 'self' の場合、まず同クラス内のメソッドを探し、なければ継承チェーンを探す
    # 実行はヒューリスティックが有効な場合のみ行う（CLIオプションで制御）。
    if _HEURISTICS_ENABLED and call.is_attr() and call.owner_token == 'self':
        parent_group = getattr(node_a, 'parent', None)
        candidates = []
        if parent_group and getattr(parent_group, 'group_type', None) == GROUP_TYPE.CLASS:
            # 1) 同クラス内のメソッドを優先
            for node in getattr(parent_group, 'nodes', []):
                if node.token == call.token:
                    candidates.append(node)

            if candidates:
                # 同クラス内に見つかったら、それを返す（通常は一意）
                if len(candidates) == 1:
                    logging.debug(f"[_find_link_for_call] 同クラスによってマッチ: {candidates[0].token} parent={getattr(candidates[0].parent,'token',None)}")
                    return candidates[0], None, None
                else:
                    logging.debug(f"[_find_link_for_call] 同クラス内で複数候補: {[c.token for c in candidates]}")
                    return None, call, (candidates, [])

            # 2) 継承チェーン内を検索
            for inherit_nodes in getattr(parent_group, 'inherits', []):
                for candidate in inherit_nodes:
                    if candidate.token == call.token:
                        candidates.append(candidate)

            # 3) ネストされたクラス（subgroup）を探して、そのコンストラクタを返す
            for subgroup in getattr(parent_group, 'subgroups', []):
                if subgroup.token == call.token:
                    ctor = subgroup.get_constructor()
                    if ctor:
                        logging.debug(f"[_find_link_for_call] ネストクラスのコンストラクタによってマッチ: {ctor.token} parent={getattr(ctor.parent,'token',None)}")
                        return ctor, None, None
                    else:
                        # Synthesize a constructor node for the nested class so
                        # calls like `self.Inner()` can point to `Inner.__init__()`.
                        synth_ctor = Node(token='__init__', calls=[], variables=None,
                                         parent=subgroup, import_tokens=[],
                                         line_number=None, is_constructor=True,
                                         is_library=False, missing=False,
                                         implicit_constructor=True)
                        subgroup.add_node(synth_ctor)
                        # Add to the global node list so subsequent resolution can find it
                        try:
                            all_nodes.append(synth_ctor)
                        except Exception:
                            pass
                        logging.debug(f"[_find_link_for_call] 合成コンストラクタを作成: {synth_ctor.token} parent={subgroup.token}")
                        return synth_ctor, None, None

        if candidates:
            if len(candidates) == 1:
                logging.debug(f"[_find_link_for_call] 継承によってマッチ: {candidates[0].token} parent={getattr(candidates[0].parent,'token',None)}")
                return candidates[0], None, None
            else:
                logging.debug(f"[_find_link_for_call] 継承候補が複数見つかりました: {[c.token for c in candidates]}")
                return None, call, (candidates, [])

    # 1. 直接のライブラリ呼び出しをチェック（例：argparse.ArgumentParser()）
    lib_node = _find_library_node_by_signature(call, all_nodes)
    if lib_node:
        return lib_node, None, None

    # Heuristic: sometimes the attribute call shows up with an unknown owner
    # (e.g. `UNKNOWN_VAR.fromutc()`) while a separate `super()` call exists
    # in the same function body. If so, and the parent class inherits a
    # matching method, treat this attribute call as coming from `super()`.
    # NOTE: This heuristic relies on the resolver having access to the
    # parent class's inherited methods (via `parent_group.inherits`). If
    # the parent class is external/unparsed, this heuristic cannot match
    # the inherited method and will not produce a `via super` link.
    if _HEURISTICS_ENABLED and call.is_attr() and (call.owner_token == OWNER_CONST.UNKNOWN_VAR or not call.owner_token):
        parent_group = getattr(node_a, 'parent', None)
        if parent_group and getattr(parent_group, 'group_type', None) == GROUP_TYPE.CLASS:
            candidates = []
            for inherit_nodes in getattr(parent_group, 'inherits', []):
                for candidate in inherit_nodes:
                    if candidate.token == call.token:
                        candidates.append(candidate)
            if candidates:
                # Check whether a `super` call exists in this node's calls
                has_super = any((getattr(c, 'token', None) == 'super') or (getattr(c, 'owner_token', None) == 'super') for c in (node_a.calls or []))
                if has_super:
                    if len(candidates) == 1:
                        try:
                            call.via_super = True
                        except Exception:
                            pass
                        logging.debug(f"[_find_link_for_call] heuristic: UNKNOWN owner resolved via super to {candidates[0].token} parent={getattr(candidates[0].parent,'token',None)}")
                        return candidates[0], None, None
                    else:
                        return None, call, (candidates, [])

    all_vars = node_a.get_variables(call.line_number)

    # 2. ライブラリインスタンス経由の呼び出しをチェック（例：parser.add_argument()）
    if call.is_attr() and call.owner_token:
        logging.debug(f"[_find_link_for_call] ライブラリインスタンス経由の呼び出しチェック: call.owner_token={call.owner_token}, call.token={call.token}")
        for var in all_vars:
            if call.owner_token == var.token:
                logging.debug(f"[_find_link_for_call] 変数マッチ: var.token={var.token}, var.points_to型={type(var.points_to).__name__}")
                lib_node = _find_library_node_from_variable(call, var, all_nodes)
                if lib_node:
                    return lib_node, None, None

    # 3. 変数マッチングによる解決
    for var in all_vars:
        var_match = call.matches_variable(var)
        if not var_match:
            continue
        
        # 未知のモジュールはマッチしないようにする
        if var_match == OWNER_CONST.UNKNOWN_MODULE:
            return None, None, None
        
        # ライブラリ呼び出しで変数がマッチした場合でも、ライブラリノードが存在すればそれを優先する
        lib_node = _find_library_node_by_signature(call, all_nodes)
        if lib_node:
            return lib_node, None, None
        
        return var_match, None, None

    # If this call was produced by calling a factory (e.g. `factory()(5)`),
    # try to resolve the factory function's return tokens (or lambda) and
    # map the outer call to the returned function/lambda.
    try:
        factory_name = getattr(call, 'factory_call', None)
    except Exception:
        factory_name = None
    if factory_name:
        # Find the factory node in all_nodes
        factory_nodes = [n for n in all_nodes if n.token == factory_name]
        if factory_nodes:
            # Prefer file-level function node
            factory_node = next((n for n in factory_nodes if isinstance(n.parent, Group) and n.parent.group_type == GROUP_TYPE.FILE), factory_nodes[0])
            # If the factory returns a named function, try to resolve that
            ret_tokens = getattr(factory_node, 'return_tokens', []) or []
            for rt in ret_tokens:
                target = next((n for n in all_nodes if n.token == rt), None)
                if target:
                    try:
                        call.via_factory = True
                    except Exception:
                        pass
                    return target, None, None
            # If factory returns a lambda, synthesize a lambda node under factory's parent
            if getattr(factory_node, 'returns_lambda', False):
                synth_token = f"{factory_node.token}::<lambda>"
                synth_ctor = Node(token=synth_token, calls=[], variables=None,
                                 parent=factory_node.parent, import_tokens=[],
                                 line_number=None, is_constructor=False,
                                 is_library=False, missing=False)
                try:
                    factory_node.parent.add_node(synth_ctor)
                except Exception:
                    pass
                try:
                    all_nodes.append(synth_ctor)
                except Exception:
                    pass
                try:
                    call.via_factory = True
                except Exception:
                    pass
                return synth_ctor, None, None

    # 4. 直接的なノードマッチング
    possible_nodes = []
    impossible_nodes = []

    if call.is_attr():
        node_a_file_group = node_a.file_group()
        for node in all_nodes:
            if call.token == node.token and node.parent != node_a_file_group:
                possible_nodes.append(node)
            else:
                impossible_nodes.append((node, 1))
    else:
        for node in all_nodes:
            if call.token == node.token and isinstance(node.parent, Group) and node.parent.group_type == GROUP_TYPE.FILE:
                possible_nodes.append(node)
            elif call.token == node.parent.token and node.is_constructor:
                possible_nodes.append(node)
            else:
                impossible_nodes.append((node, 2))

    # If we didn't find an explicit target node but a class group with the
    # same name exists, treat ClassName() as calling its constructor. If the
    # constructor is not defined, synthesize an implicit constructor node and
    # mark it as an implicit constructor so the graph shows that fact.
    if not possible_nodes:
        for node in all_nodes:
            parent = getattr(node, 'parent', None)
            try:
                is_class_grp = isinstance(parent, Group) and parent.group_type == GROUP_TYPE.CLASS
            except Exception:
                is_class_grp = False
            if is_class_grp and parent.token == call.token:
                ctor = parent.get_constructor()
                if ctor:
                    logging.debug(f"[_find_link_for_call] Matched class constructor: {ctor.token} parent={getattr(ctor.parent,'token',None)}")
                    return ctor, None, None
                # Synthesize implicit constructor for this class
                synth_ctor = Node(token='__init__', calls=[], variables=None,
                                 parent=parent, import_tokens=[], line_number=None,
                                 is_constructor=True, is_library=False, missing=False,
                                 implicit_constructor=True)
                parent.add_node(synth_ctor)
                try:
                    all_nodes.append(synth_ctor)
                except Exception:
                    pass
                logging.debug(f"[_find_link_for_call] Synthesized implicit constructor: {synth_ctor.token} parent={parent.token}")
                return synth_ctor, None, None

    if len(possible_nodes) == 1:
        return possible_nodes[0], None, None
    if len(possible_nodes) > 1:
        return None, call, (possible_nodes, impossible_nodes)
    return None, None, None


def _find_links(node_a: Node, all_nodes):
    """
    Iterate through the calls on node_a to find everything the node links to.
    This will return a list of tuples of nodes and calls that were ambiguous.\n
    node_aからのリンク先を抽出する。

    :param Node node_a:
    :param list[Node] all_nodes:
    :param BaseLanguage language:
    :rtype: list[(Node, Call)]
    """

    links = []

    if node_a.calls is None:
        pass

    for call in node_a.calls:
        # node_aがライブラリの場合はここでNoneType is not iterableで怒られる。
        # 関数は呼び先がない場合もcallsには空のリストが割り当たっているのでNode生成時にライブラリもそうすべき。
        if call.owner_token is not None:
            pass

        _possible_node, _call, _nodes = _find_link_for_call(call, node_a, all_nodes)
        # Return triple: (resolved_node_or_None, bad_call_or_None, original_call)
        lfc = (_possible_node, _call, call)
        links.append(lfc)
    return list(filter(None, links))


def map_it(sources, extension, no_trimming, exclude_namespaces, exclude_functions,
           include_only_namespaces, include_only_functions,
           skip_parse_errors, lang_params, alias_labels=False, heuristics=True,
           show_libraries=False):

    """
    Given a language implementation and a list of filenames, do these things:
    1. Read/parse source ASTs
    2. Find all groups (classes/modules) and nodes (functions) (a lot happens here)
    3. Trim namespaces / functions that we don't want
    4. Consolidate groups / nodes given all we know so far
    5. Attempt to resolve the variables (point them to a node or group)
    6. Find all calls between all nodes
    7. Loudly complain about duplicate edges that were skipped
    8. Trim nodes that didn't connect to anything

    :param list[str] sources:
    :param str extension:
    :param bool no_trimming:
    :param list exclude_namespaces:
    :param list exclude_functions:
    :param list include_only_namespaces:
    :param list include_only_functions:
    :param bool skip_parse_errors:
    :param LanguageParams lang_params:

    :rtype: (list[Group], list[Node], list[Edge])
    """

    # ToDo: ここの関数でimportモジュールをnodeに詰めて、呼び出し関係をedgeに詰めれば勝ち

    language = LANGUAGES[extension]

    # 0. Assert dependencies
    language.assert_dependencies()

    # 1. Read/parse source ASTs
    file_ast_trees: list[(str, ast.AST)] = []
    for source in sources:
        try:
            file_ast_trees.append((source, language.get_tree(source, lang_params)))
        except Exception as ex:
            if skip_parse_errors:
                logging.warning("Could not parse %r. (%r) Skipping...", source, ex)
            else:
                raise ex

    # 2. Find all groups (classes/modules), nodes (functions) (a lot happens here) and external-modules
    # Use recursive parsing to include local imported modules so that
    # imports like `from exclude_modules_b import match` get their definitions
    # included in the file_groups for resolution later.
    file_groups = []
    parsed_files = set()
    # Heuristics toggle: when enabled, recursively follow local imports. When
    # disabled, only parse the explicit source files provided on the CLI.
    global _HEURISTICS_ENABLED
    _HEURISTICS_ENABLED = bool(heuristics)

    for source, _file_ast_tree in file_ast_trees:
        base_dir = os.path.dirname(source) or '.'
        try:
            if _HEURISTICS_ENABLED:
                groups = parse_file_recursive(source, base_dir, parsed_files, extension)
                for g in groups:
                    file_groups.append(g)
            else:
                # Only parse the explicit file; do not follow imports.
                file_groups.append(make_file_group(_file_ast_tree, source, extension))
        except Exception as ex:
            if skip_parse_errors:
                logging.warning("Could not parse dependency tree for %r (%r). Skipping...", source, ex)
            else:
                raise

    # 3. Trim namespaces / functions to exactly what we want
    if exclude_namespaces or include_only_namespaces:
        file_groups = _limit_namespaces(file_groups, exclude_namespaces, include_only_namespaces)
    if exclude_functions or include_only_functions:
        file_groups = _limit_functions(file_groups, exclude_functions, include_only_functions)

    # 4. Consolidate structures
    # file_groupsに階層化してあるnodesとsubgroupsをここで同一階層に展開する。
    unflatten_g_groups = [g.all_groups() for g in file_groups]
    all_subgroups = flatten(unflatten_g_groups)

    unflatten_g_nodes = [g.all_nodes() for g in file_groups]
    all_nodes = flatten(unflatten_g_nodes)

    unflatten_g_imports = [g.all_imports() for g in file_groups]
    all_imports = flatten(unflatten_g_imports)

    nodes_by_subgroup_token = collections.defaultdict(list)
    # Use hierarchical subgroup keys to avoid token collisions for nested classes.
    def subgroup_full_token(sg: Group):
        parts = []
        cur = sg
        # Walk up until we reach the file group
        while cur and getattr(cur, 'group_type', None) != GROUP_TYPE.FILE:
            parts.insert(0, cur.token)
            cur = cur.parent
        if cur and getattr(cur, 'group_type', None) == GROUP_TYPE.FILE:
            parts.insert(0, cur.token)
        return djoin(*parts)

    for subgroup in all_subgroups:
        full = subgroup_full_token(subgroup)
        if full in nodes_by_subgroup_token:
            logging.warning("Duplicate group full token %r. Naming collision possible.", full)
        nodes_by_subgroup_token[full] += subgroup.nodes

    for group in file_groups:
        for subgroup in group.all_groups():
            # Populate inheritance links and inject inherited methods as
            # variables into subclasses only when heuristics are enabled.
            if _HEURISTICS_ENABLED:
                resolved_inherits = []
                for inh in list(subgroup.inherits):
                    # inh is a bare name (from AST); match against hierarchical keys.
                    matched = []
                    for key, nodes_list in nodes_by_subgroup_token.items():
                        # key endswith the class name?
                        if key.split('.')[-1] == inh:
                            matched.append(nodes_list)
                    if matched:
                        # flatten matched lists and append
                        for ml in matched:
                            resolved_inherits.append(ml)
                subgroup.inherits = resolved_inherits
                for inherit_nodes in subgroup.inherits:
                    for node in subgroup.nodes:
                        node.variables += [Variable(n.token, n, n.line_number) for n in inherit_nodes]
            else:
                # If heuristics disabled, keep original inherited token names (unresolved)
                subgroup.inherits = []

    # 5. Attempt to resolve the variables (point them to a node or group)
    for node in all_nodes:
        if node.variables is not None:
            node.resolve_variables(file_groups)
        # Todo:↑にfile_groupだけではなくlib_groupも追加して解決してやる必要があるのでは？
        #      file_groupの要素にimportsも入っているので大丈夫のはず。
        

    # Argument-propagation heuristic: if a call passes a function name as a
    # positional arg into another function (e.g. `trace(do_something)`), then
    # propagate that argument into the callee's parameter variable so that
    # later `fn()` calls inside the callee can resolve to the passed function.
    if _HEURISTICS_ENABLED:
        for node in all_nodes:
            if not node.calls:
                continue
            for call in node.calls:
                arg_tokens = getattr(call, 'arg_tokens', None) or []
                if not arg_tokens:
                    continue
                # Find the target function node for this call (simple file-level match)
                candidates = [n for n in all_nodes if n.token == call.token and isinstance(n.parent, Group) and n.parent.group_type == GROUP_TYPE.FILE]
                if len(candidates) != 1:
                    continue
                target = candidates[0]
                param_tokens = getattr(target, 'param_tokens', [])
                if not param_tokens:
                    continue
                # Map positional args -> params by index
                for i, arg_tok in enumerate(arg_tokens):
                    if i >= len(param_tokens):
                        break
                    param_name = param_tokens[i]
                    # find a node that matches the argument token
                    arg_node = next((n for n in all_nodes if n.token == arg_tok), None)
                    if not arg_node:
                        continue
                    # find the variable in the target function for this parameter
                    if target.variables:
                        for var in target.variables:
                            if var.token == param_name:
                                var.points_to = arg_node
                                logging.debug(f"[arg-prop] Propagated arg {arg_tok} -> {target.token}.{param_name}")
                                break

    # Temporary debug: inspect any Variable named 'arg_parser' or 'parser'
    try:
        for node in all_nodes:
            vars_list = getattr(node, 'variables', None) or []
            for var in vars_list:
                try:
                    if var.token in ('arg_parser', 'parser'):
                        pts = getattr(var, 'points_to', None)
                        if pts is None:
                            logging.info(f"[DEBUG_VAR] {var.token} -> points_to is None (node={getattr(node,'token',None)})")
                        else:
                            # If it's a Call, show owner_token/token/is_library
                            from .model import Call as _Call, Node as _Node, Group as _Group
                            if isinstance(pts, _Call):
                                logging.info(f"[DEBUG_VAR] {var.token} -> Call owner_token={getattr(pts,'owner_token',None)} token={getattr(pts,'token',None)} is_library={getattr(pts,'is_library',False)} (node={getattr(node,'token',None)})")
                            elif isinstance(pts, _Node):
                                logging.info(f"[DEBUG_VAR] {var.token} -> Node token={getattr(pts,'token',None)} parent={getattr(getattr(pts,'parent',None),'token',None)} is_library={getattr(pts,'is_library',False)} (node={getattr(node,'token',None)})")
                            elif isinstance(pts, _Group):
                                logging.info(f"[DEBUG_VAR] {var.token} -> Group token={getattr(pts,'token',None)} display_type={getattr(pts,'display_type',None)} (node={getattr(node,'token',None)})")
                            else:
                                logging.info(f"[DEBUG_VAR] {var.token} -> points_to type={type(pts).__name__} repr={pts!r} (node={getattr(node,'token',None)})")
                except Exception:
                    logging.exception("[DEBUG_VAR] error while inspecting variable")
    except Exception:
        logging.exception("[DEBUG_VAR] error iterating variables")

    # Not a step. Just log what we know so far
    logging.info("Found groups %r." % [g.label() for g in all_subgroups])
    logging.info("Found nodes %r." % sorted(n.token_with_ownership() for n in all_nodes))
    unflatten_n_calls = [n.calls for n in all_nodes]
    logging.info("Found calls %r." % sorted(list(set(c.to_string() for c in flatten(unflatten_n_calls)))))
    logging.info("Found variables %r." % sorted(list(set(v.to_string() for v in
                                                         flatten(n.variables for n in all_nodes)))))

    # 5.5. ライブラリ呼び出しからライブラリ関数のノードを作成する
    # すべてのライブラリ呼び出しを収集し、それらのノードを作成する
    library_nodes_by_signature = {}
    library_groups_by_module = {}
    # Builtin names (e.g. print, len) should be treated as library functions
    # Use a callable filter so we only consider callable builtins (functions/types)
    # and exclude non-callable names (constants, dunder names that aren't callables, etc.).
    try:
        builtin_names = {name for name, val in builtins.__dict__.items() if callable(val)}
    except Exception:
        # Fallback to the original approach if something unexpected happens
        builtin_names = set(dir(builtins))
    
    unflatten_n_calls_all = [n.calls for n in all_nodes if n.calls]
    all_calls = flatten(unflatten_n_calls_all)
    
    for call in all_calls:
        if call.is_library and call.is_attr() and call.owner_token:
            # ライブラリ関数のシグネチャを作成（例："requests.get"）
            lib_signature = djoin(call.owner_token, call.token)
            
            if lib_signature not in library_nodes_by_signature:
                # モジュール名を抽出（owner_tokenの最初の部分）
                module_name = call.owner_token.split('.')[0] if '.' in call.owner_token else call.owner_token
                
                # ライブラリモジュールのグループを作成または取得
                if module_name not in library_groups_by_module:
                    # Use a special display type for Python builtins so they
                    # are shown under a "Built-in Functions" cluster instead
                    # of the generic Library cluster.
                    display_type = "Built-in Functions" if module_name == 'builtins' else "Library"
                    lib_group = Group(
                        token=module_name,
                        group_type=GROUP_TYPE.NAMESPACE,
                        display_type=display_type,
                        import_tokens=[module_name],
                        line_number=0,
                        parent=None
                    )
                    library_groups_by_module[module_name] = lib_group
                
                lib_group = library_groups_by_module[module_name]
                
                # ライブラリ関数のノードを作成
                lib_node = Node(
                    token=call.token,
                    calls=[],
                    variables=None,
                    parent=lib_group,
                    import_tokens=[lib_signature],
                    line_number=call.line_number,
                    is_constructor=False,
                    is_library=True
                )
                
                lib_group.add_node(lib_node)
                library_nodes_by_signature[lib_signature] = lib_node
                # Ensure synthesized library nodes are visible to the resolver
                try:
                    all_nodes.append(lib_node)
                except Exception:
                    pass

    # Handle instance-method calls on variables that point to library constructors.
    # Example: `arg_parser = argparse.ArgumentParser()` then `arg_parser.add_mutually_exclusive_group()`
    # In this case, synthesize a library node for `argparse.add_mutually_exclusive_group` so
    # instance-method calls can be linked to the appropriate library function node.
    try:
        for call in all_calls:
            try:
                if not (call.is_attr() and call.owner_token):
                    continue
                owner_token = call.owner_token
                # Skip if owner_token already looks like a module (dotted) or is itself a module name
                # We are interested in variables (simple names) that reference library instances.
                if '.' in owner_token:
                    continue
                # Find a Variable with this token across all nodes
                found_var = None
                for node in all_nodes:
                    vars_list = getattr(node, 'variables', None) or []
                    for var in vars_list:
                        if var.token == owner_token:
                            found_var = var
                            break
                    if found_var:
                        break
                if not found_var:
                    continue
                pts = getattr(found_var, 'points_to', None)
                # We only care about variables whose points_to is a library Call
                if not isinstance(pts, Call) or not getattr(pts, 'is_library', False):
                    continue
                # Derive module name from the constructor call owner_token
                module_name = pts.owner_token.split('.')[0] if '.' in pts.owner_token else pts.owner_token
                lib_signature = djoin(module_name, call.token)
                if lib_signature in library_nodes_by_signature:
                    continue
                # Ensure a library group exists for this module
                if module_name not in library_groups_by_module:
                    display_type = "Built-in Functions" if module_name == 'builtins' else "Library"
                    lib_group = Group(
                        token=module_name,
                        group_type=GROUP_TYPE.NAMESPACE,
                        display_type=display_type,
                        import_tokens=[module_name],
                        line_number=0,
                        parent=None
                    )
                    library_groups_by_module[module_name] = lib_group
                lib_group = library_groups_by_module[module_name]
                lib_node = Node(
                    token=call.token,
                    calls=[],
                    variables=None,
                    parent=lib_group,
                    import_tokens=[lib_signature],
                    line_number=call.line_number,
                    is_constructor=False,
                    is_library=True
                )
                lib_group.add_node(lib_node)
                library_nodes_by_signature[lib_signature] = lib_node
                # Debug: report that we synthesized an instance-based library node
                logging.debug(f"[LIB_SYNTH] synthesized {lib_signature} -> node token={lib_node.token} parent={lib_group.token}")
            except Exception:
                logging.exception("Error while synthesizing instance-based library node for call %r", getattr(call, 'to_string', lambda: call)())
    except Exception:
        logging.exception("Unexpected error during instance-method library synthesis")
    
    # ライブラリグループを file_groups に追加し、ライブラリノードを all_nodes に追加する
    # Special-case: place the 'builtins' module in its own top-level file group
    builtins_file_group = None
    for lib_group in library_groups_by_module.values():
        if lib_group.token == 'builtins':
            # Create or reuse a top-level file_group for builtins so it doesn't
            # appear as a subgroup of the analyzed source file (e.g. __init__.py).
            if builtins_file_group is None:
                builtins_file_group = Group(token='builtins', group_type=GROUP_TYPE.FILE,
                                            display_type='File', import_tokens=[], line_number=0, parent=None)
                file_groups.append(builtins_file_group)
            builtins_file_group.add_subgroup(lib_group)
            lib_group.parent = builtins_file_group
        else:
            # Attach other library groups as pseudo-subgroups of the first file_group
            if file_groups:
                file_groups[0].add_subgroup(lib_group)
                lib_group.parent = file_groups[0]
        all_nodes.extend(lib_group.all_nodes())

    # 6. Find all calls between all nodes
    bad_calls = []
    edges = []
    # Collect synthesized nodes for unresolved calls and place them in a special group
    missing_nodes_by_key = {}
    missing_group = Group(token='NotFound', group_type=GROUP_TYPE.FILE, display_type='File', import_tokens=[], line_number=0, parent=None)
    for node_a in list(all_nodes):
        # ToDo: ここのall_nodesに外部モジュールの関数を呼び出している部分が入っていないので入れる。
        links = _find_links(node_a, all_nodes + all_imports)
        for node_b, bad_call, call in links:
            if bad_call:
                bad_calls.append(bad_call)
            if not node_b:
                # If the unresolved call is a builtin function (like `print()`),
                # synthesize or reuse a library node under the `builtins` module
                # instead of creating a generic NotFound node.
                try:
                    is_builtin = (call.owner_token is None) and (call.token in builtin_names)
                except Exception:
                    is_builtin = False

                if is_builtin:
                    module_name = 'builtins'
                    lib_sig = djoin(module_name, call.token)
                    # Ensure a builtin library group exists
                    if module_name not in library_groups_by_module:
                        display_type = "Built-in Functions" if module_name == 'builtins' else "Library"
                        lib_group = Group(
                            token=module_name,
                            group_type=GROUP_TYPE.NAMESPACE,
                            display_type=display_type,
                            import_tokens=[module_name],
                            line_number=0,
                            parent=None
                        )
                        library_groups_by_module[module_name] = lib_group
                    lib_group = library_groups_by_module[module_name]

                    # Reuse existing synthesized builtin node if present
                    if lib_sig in library_nodes_by_signature:
                        node_b = library_nodes_by_signature[lib_sig]
                    else:
                        lib_node = Node(
                            token=call.token,
                            calls=[],
                            variables=None,
                            parent=lib_group,
                            import_tokens=[lib_sig],
                            line_number=call.line_number,
                            is_constructor=False,
                            is_library=True
                        )
                        lib_group.add_node(lib_node)
                        library_nodes_by_signature[lib_sig] = lib_node
                        all_nodes.append(lib_node)
                        node_b = lib_node

                    # Ensure the builtin library group is attached to a file_group for output
                    if file_groups and lib_group.parent is None:
                        # Prefer attaching builtins to their own top-level file group
                        if lib_group.token == 'builtins':
                            # Find existing builtins file group or create one
                            existing = next((g for g in file_groups if g.token == 'builtins' and g.group_type == GROUP_TYPE.FILE), None)
                            if existing is None:
                                existing = Group(token='builtins', group_type=GROUP_TYPE.FILE, display_type='File', import_tokens=[], line_number=0, parent=None)
                                file_groups.append(existing)
                            existing.add_subgroup(lib_group)
                            lib_group.parent = existing
                        else:
                            file_groups[0].add_subgroup(lib_group)
                            lib_group.parent = file_groups[0]
                    # proceed to create edge to node_b (builtin)
                    
                else:
                    # Unresolved call: synthesize a NotFound node and link to it
                    try:
                        key = call.to_string()
                    except Exception:
                        # Fallback key
                        key = (call.owner_token + '.' + call.token) if call.owner_token else call.token

                    if key not in missing_nodes_by_key:
                        missing_node = Node(token=call.token, calls=[], variables=None, parent=missing_group,
                                            import_tokens=[], line_number=None, is_constructor=False,
                                            is_library=False, missing=True)
                        missing_group.add_node(missing_node)
                        all_nodes.append(missing_node)
                        missing_nodes_by_key[key] = missing_node

                    node_b = missing_nodes_by_key[key]

            edge = Edge(node_a, node_b)
            # If this call was resolved via `super()`, annotate the edge.
            # This initial assignment may be overwritten by alias labeling; we
            # append '(via super)' afterwards to preserve the information.
            try:
                if getattr(call, 'via_super', False):
                    edge.label = 'via super'
            except Exception:
                pass
            if alias_labels:
                try:
                    if call:
                        vars_at_line = node_a.get_variables(call.line_number)
                        # 1) Non-attribute calls: same as before
                        if not call.is_attr():
                            for var in vars_at_line:
                                if var.token == call.token:
                                    # If the variable points to the node we're linking to,
                                    # annotate the edge with the alias used.
                                    if var.points_to == node_b or (isinstance(var.points_to, Group) and node_b in var.points_to.all_nodes()):
                                        edge.label = f"as {call.token}()"
                                        break
                        else:
                            # 2) Attribute calls like `obj.method()` — find variable matching owner_token
                            owner = call.owner_token
                            for var in vars_at_line:
                                try:
                                    if var.token != owner:
                                        continue
                                except Exception:
                                    continue
                                # If variable points to the node/group we're linking to,
                                # label the edge with the alias used: `as obj.method()`
                                if var.points_to == node_b or (isinstance(var.points_to, Group) and node_b in var.points_to.all_nodes()):
                                    edge.label = f"as {var.token}.{call.token}()"
                                    break
                except Exception:
                    # Be conservative: ignore labeling errors and emit unlabeled edge
                    pass
            # Ensure 'via super' is preserved/appended even when alias labels are used
            try:
                if getattr(call, 'via_super', False):
                    if edge.label:
                        edge.label = f"{edge.label} (via super)"
                    else:
                        edge.label = 'via super'
            except Exception:
                pass
            # Preserve/append factory and dict indirect labels too
            try:
                if getattr(call, 'via_factory', False):
                    if edge.label:
                        edge.label = f"{edge.label} (via factory)"
                    else:
                        edge.label = 'via factory'
            except Exception:
                pass
            try:
                indirect = getattr(call, 'indirect', None)
                if indirect and indirect[0] == 'dict':
                    # indirect == ('dict', var_name, key)
                    _, var_name, key = indirect
                    label = f"as {var_name}['{key}']()"
                    if edge.label:
                        edge.label = f"{edge.label} {label}"
                    else:
                        edge.label = label
            except Exception:
                pass
            edges.append(edge)

    # If we created any missing nodes, ensure their group is included for output
    if missing_group.nodes:
        file_groups.append(missing_group)

    # 7. Loudly complain about duplicate edges that were skipped
    bad_calls_strings = set()
    for bad_call in bad_calls:
        bad_calls_strings.add(bad_call.to_string())
    bad_calls_strings = list(sorted(list(bad_calls_strings)))
    if bad_calls_strings:
        logging.info("Skipped processing these calls because the algorithm "
                     "linked them to multiple function definitions: %r." % bad_calls_strings)

    if no_trimming:
        return file_groups, all_nodes, edges

    # Optionally remove library groups/nodes/edges from the output
    if not show_libraries:
        lib_nodes = {n for n in all_nodes if getattr(n, 'is_library', False)}
        if lib_nodes:
            # remove edges that reference library nodes
            edges = [e for e in edges if e.node0 not in lib_nodes and e.node1 not in lib_nodes]
            # remove library nodes from their parents
            for n in list(lib_nodes):
                try:
                    n.remove_from_parent()
                except Exception:
                    pass
            # remove library subgroups from file_groups
            for fg in list(file_groups):
                # remove subgroups whose display_type indicates library
                for sg in list(fg.subgroups):
                    if getattr(sg, 'display_type', '') in ('Library', 'Built-in Functions'):
                        try:
                            fg.subgroups.remove(sg)
                        except Exception:
                            pass
            # update all_nodes to exclude libraries
            all_nodes = [n for n in all_nodes if n not in lib_nodes]

    # 8. Trim nodes that didn't connect to anything
    nodes_with_edges = set()
    for edge in edges:
        nodes_with_edges.add(edge.node0)
        nodes_with_edges.add(edge.node1)

    for node in all_nodes:
        if node not in nodes_with_edges:
            node.remove_from_parent()

    for file_group in file_groups:
        for group in file_group.all_groups():
            if not group.all_nodes():
                group.remove_from_parent()

    file_groups = [g for g in file_groups if g.all_nodes()]
    all_nodes = list(nodes_with_edges)

    if not all_nodes:
        logging.warning("No functions found! Most likely, your file(s) do not have "
                        "functions that call each other. Note that to generate a flowchart, "
                        "you need to have both the function calls and the function "
                        "definitions. Or, you might be excluding too many "
                        "with --exclude-* / --include-* / --target-function arguments. ")
        logging.warning("Code2flow will generate an empty output file.")

    return file_groups, all_nodes, edges


def _limit_namespaces(file_groups, exclude_namespaces, include_only_namespaces):
    """
    Exclude namespaces (classes/modules) which match any of the exclude_namespaces

    :param list[Group] file_groups:
    :param list exclude_namespaces:
    :param list include_only_namespaces:
    :rtype: list[Group]
    """

    removed_namespaces = set()

    for group in list(file_groups):
        if group.token in exclude_namespaces:
            for node in group.all_nodes():
                node.remove_from_parent()
            removed_namespaces.add(group.token)
        if include_only_namespaces and group.token not in include_only_namespaces:
            for node in group.nodes:
                node.remove_from_parent()
            removed_namespaces.add(group.token)

        for subgroup in group.all_groups():
            print(subgroup, subgroup.all_parents())
            if subgroup.token in exclude_namespaces:
                for node in subgroup.all_nodes():
                    node.remove_from_parent()
                removed_namespaces.add(subgroup.token)
            if include_only_namespaces and \
               subgroup.token not in include_only_namespaces and \
               all(p.token not in include_only_namespaces for p in subgroup.all_parents()):
                for node in subgroup.nodes:
                    node.remove_from_parent()
                removed_namespaces.add(group.token)

    for namespace in exclude_namespaces:
        if namespace not in removed_namespaces:
            logging.warning(f"Could not exclude namespace '{namespace}' "
                             "because it was not found.")
    return file_groups


def _limit_functions(file_groups, exclude_functions, include_only_functions):
    """
    Exclude nodes (functions) which match any of the exclude_functions

    :param list[Group] file_groups:
    :param list exclude_functions:
    :param list include_only_functions:
    :rtype: list[Group]
    """

    removed_functions = set()

    for group in list(file_groups):
        for node in group.all_nodes():
            if node.token in exclude_functions or \
               (include_only_functions and node.token not in include_only_functions):
                node.remove_from_parent()
                removed_functions.add(node.token)

    for function_name in exclude_functions:
        if function_name not in removed_functions:
            logging.warning(f"Could not exclude function '{function_name}' "
                             "because it was not found.")
    return file_groups


def _generate_graphviz(output_file, extension, final_img_filename):
    """
    Write the graphviz file
    :param str output_file:
    :param str extension:
    :param str final_img_filename:
    """
    start_time = time.time()
    logging.info("Running graphviz to make the image...")
    command = ["dot", "-T" + extension, output_file]
    with open(final_img_filename, 'w') as f:
        try:
            subprocess.run(command, stdout=f, check=True)
            logging.info("Graphviz finished in %.2f seconds." % (time.time() - start_time))
        except subprocess.CalledProcessError:
            logging.warning("*** Graphviz returned non-zero exit code! "
                            "Try running %r for more detail ***", ' '.join(command + ['-v', '-O']))


def _generate_final_img(output_file, extension, final_img_filename, num_edges):
    """
    Write the graphviz file
    :param str output_file:
    :param str extension:
    :param str final_img_filename:
    :param int num_edges:
    """
    _generate_graphviz(output_file, extension, final_img_filename)
    logging.info("Completed your flowchart! To see it, open %r.",
                 final_img_filename)


def code2flow(raw_source_paths, output_file, language=None, hide_legend=True,
              exclude_namespaces=None, exclude_functions=None,
              include_only_namespaces=None, include_only_functions=None,
              no_grouping=False, no_trimming=False, skip_parse_errors=False,
              lang_params=None, subset_params=None, alias_labels=False, level=logging.INFO,
              heuristics=True, show_libraries=False):
    """
    Top-level function. Generate a diagram based on source code.
    Can generate either a dotfile or an image.

    :param list[str] raw_source_paths: file or directory paths
    :param str|file output_file: path to the output file. SVG/PNG will generate an image.
    :param str language: input language extension
    :param bool hide_legend: Omit the legend from the output
    :param list exclude_namespaces: List of namespaces to exclude
    :param list exclude_functions: List of functions to exclude
    :param list include_only_namespaces: List of namespaces to include
    :param list include_only_functions: List of functions to include
    :param bool no_grouping: Don't group functions into namespaces in the final output
    :param bool no_trimming: Don't trim orphaned functions / namespaces
    :param bool skip_parse_errors: If a language parser fails to parse a file, skip it
    :param lang_params LanguageParams: Object to store lang-specific params
    :param subset_params SubsetParams: Object to store subset-specific params
    :param int level: logging level
    :rtype: None
    """
    start_time = time.time()

    if not isinstance(raw_source_paths, list):
        raw_source_paths = [raw_source_paths]
    lang_params = lang_params or LanguageParams()

    exclude_namespaces = exclude_namespaces or []
    if not isinstance(exclude_namespaces, list):
        raise TypeError("exclude_namespaces must be a list")
    exclude_functions = exclude_functions or []
    if not isinstance(exclude_functions, list):
        raise TypeError("exclude_functions must be a list")
    include_only_namespaces = include_only_namespaces or []
    if not isinstance(include_only_namespaces, list):
        raise TypeError("include_only_namespaces must be a list")
    include_only_functions = include_only_functions or []
    if not isinstance(include_only_functions, list):
        raise TypeError("include_only_functions must be a list")

    logging.basicConfig(format="Code2Flow: %(message)s", level=level)

    sources, language = get_sources_and_language(raw_source_paths, language)
    # ここでソースのリストを取得、言語を推定
    # ToDo: sourcesにpipでインストールしたパッケージが含まれていない。まぁそりゃそうか？
    #  でもどの関数がどのパッケージ呼んでるかとか知りたいんだけどなぁ。
    #  AST木内のast.Importにimportしたパッケージの名前は入ってるからたどれるはず。

    output_ext = None
    if isinstance(output_file, str):
        # output_fileがstr型の場合にTrue
        # https://docs.python.org/ja/3/library/functions.html#isinstance
        if '.' not in output_file:
            raise ValueError("Output filename must end in one of: %r." % set(VALID_EXTENSIONS))

        output_ext = output_file.rsplit('.', 1)[1] or ''
        # output_fileの拡張子を取り出す。
        # 最大分割回数1回でindex=1を指定するとaaa.bbb.cccのcccが取り出せる。
        if output_ext not in VALID_EXTENSIONS:
            raise ValueError("Output filename must end in one of: %r." % set(VALID_EXTENSIONS))

    final_img_filename = None
    extension = None
    if output_ext and output_ext in IMAGE_EXTENSIONS:
        # ToDo: １個目のoutput_extいるんだっけ？ 例外出る？
        if not is_installed('dot') and not is_installed('dot.exe'):
            raise RuntimeError(
                "Can't generate a flowchart image because neither `dot` nor "
                "`dot.exe` was found. Either install graphviz (see the README) "
                "or, if you just want an intermediate text file, set your --output "
                "file to use a supported text extension: %r" % set(TEXT_EXTENSIONS))
        final_img_filename = output_file
        output_file, extension = output_file.rsplit('.', 1)
        output_file += '.gv'

    file_groups, all_nodes, edges = map_it(sources, language, no_trimming,
                                           exclude_namespaces, exclude_functions,
                                           include_only_namespaces, include_only_functions,
                                           skip_parse_errors, lang_params,
                                           alias_labels=alias_labels, heuristics=heuristics,
                                           show_libraries=show_libraries)

    if subset_params:
        logging.info("Filtering into subset...")
        file_groups, all_nodes, edges = _filter_for_subset(subset_params, all_nodes, edges, file_groups)

    file_groups.sort()
    all_nodes.sort()
    edges.sort()

    logging.info("Generating output file...")

    if isinstance(output_file, str):
        with open(output_file, 'w') as fh:
            as_json = output_ext == 'json'
            write_file(fh, nodes=all_nodes, edges=edges,
                       groups=file_groups, hide_legend=hide_legend,
                       no_grouping=no_grouping, as_json=as_json)
    else:
        write_file(output_file, nodes=all_nodes, edges=edges,
                   groups=file_groups, hide_legend=hide_legend,
                   no_grouping=no_grouping)

    logging.info("Wrote output file %r with %d nodes and %d edges.",
                 output_file, len(all_nodes), len(edges))
    if not output_ext == 'json':
        logging.info("For better machine readability, you can also try outputting in a json format.")
    logging.info("Code2flow finished processing in %.2f seconds." % (time.time() - start_time))

    # translate to an image if that was requested
    if final_img_filename:
        _generate_final_img(output_file, extension, final_img_filename, len(edges))


def main(sys_argv=None):
    """
    CLI interface. Sys_argv is a parameter for the sake of unittest coverage.
    :param sys_argv list:
    :rtype: None
    """
    arg_parser = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    arg_parser.add_argument(
        'sources', metavar='sources', nargs='+',
        help='source code file/directory paths.')
    arg_parser.add_argument(
        '--output', '-o', default='out.svg',
        help=f'output file path. Supported types are {VALID_EXTENSIONS}.')
    arg_parser.add_argument(
        '--language', choices=['py', 'js', 'rb', 'php'],
        help='process this language and ignore all other files.'
             'If omitted, use the suffix of the first source file.')
    arg_parser.add_argument(
        '--target-function',
        help='output a subset of the graph centered on this function. '
             'Valid formats include `func`, `class.func`, and `file::class.func`. '
             'Requires --upstream-depth and/or --downstream-depth. ')
    arg_parser.add_argument(
        '--upst'
        'ream-depth', type=int, default=0,
        help='include n nodes upstream of --target-function.')
    arg_parser.add_argument(
        '--downstream-depth', type=int, default=0,
        help='include n nodes downstream of --target-function.')
    arg_parser.add_argument(
        '--exclude-functions',
        help='exclude functions from the output. Comma delimited.')
    arg_parser.add_argument(
        '--exclude-namespaces',
        help='exclude namespaces (Classes, modules, etc) from the output. Comma delimited.')
    arg_parser.add_argument(
        '--include-only-functions',
        help='include only functions in the output. Comma delimited.')
    arg_parser.add_argument(
        '--include-only-namespaces',
        help='include only namespaces (Classes, modules, etc) in the output. Comma delimited.')
    arg_parser.add_argument(
        '--no-grouping', action='store_true',
        help='instead of grouping functions into namespaces, let functions float.')
    arg_parser.add_argument(
        '--no-trimming', action='store_true',
        help='show all functions/namespaces whether or not they connect to anything.')
    arg_parser.add_argument(
        '--hide-legend', action='store_true',
        help='by default, Code2flow generates a small legend. This flag hides it.')
    arg_parser.add_argument(
        '--skip-parse-errors', action='store_true',
        help='skip files that the language parser fails on.')
    arg_parser.add_argument(
        '--source-type', choices=['script', 'module'], default='script',
        help='js only. Parse the source as scripts (commonJS) or modules (es6)')
    arg_parser.add_argument(
        '--ruby-version', default='27',
        help='ruby only. Which ruby version to parse? This is passed directly into ruby-parse. '
             'Use numbers like 25, 27, or 31.')
    arg_parser.add_argument(
        '--quiet', '-q', action='store_true',
        help='suppress most logging')
    arg_parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='add more logging')
    arg_parser.add_argument(
        '--alias-labels', action='store_true',
        help='エッジに変数エイリアス経由の呼び出しをラベル表示します (例: as abra3()).')
    arg_parser.add_argument(
        '--heuristics', dest='heuristics', action='store_true',
        help='Enable resolution heuristics (recursive local imports, inheritance-aware self resolution).')
    arg_parser.add_argument(
        '--no-heuristics', dest='heuristics', action='store_false',
        help='Disable resolution heuristics; only parse explicit files and do not apply inheritance heuristics.')
    arg_parser.set_defaults(heuristics=True)
    arg_parser.add_argument(
        '--version', action='version', version='%(prog)s ' + VERSION)
    # Option to control whether imported library functions/modules are shown
    lib_group = arg_parser.add_mutually_exclusive_group()
    lib_group.add_argument(
        '--show-libraries', dest='show_libraries', action='store_true',
        help='include imported library functions/modules in the generated graph')
    lib_group.add_argument(
        '--no-libraries', dest='show_libraries', action='store_false',
        help='do not include imported library functions/modules in the generated graph')
    arg_parser.set_defaults(show_libraries=False)

    sys_argv = sys_argv or sys.argv[1:]

    # Support simple key=value overrides on the command line, e.g.
    #   python -m code2flow <sources> level=DEBUG
    # This extracts known overrides and removes them from argv before
    # handing the rest to argparse.
    explicit_level = None
    filtered_argv = []
    for item in sys_argv:
        if isinstance(item, str) and item.startswith('level='):
            val = item.split('=', 1)[1]
            # allow either 'DEBUG' or 'logging.DEBUG'
            if val.startswith('logging.'):
                val = val.split('.', 1)[1]
            # numeric level accepted
            try:
                explicit_level = int(val)
            except Exception:
                lvl = val.upper()
                explicit_level = getattr(logging, lvl, None)
                if explicit_level is None:
                    logging.warning("Unknown logging level %r passed via CLI; ignoring.", val)
            continue
        filtered_argv.append(item)

    sys_argv = filtered_argv

    # Temporary debug: report what local names 'parser' and 'arg_parser' refer to
    try:
        _parser_val = parser
    except NameError:
        print("DEBUG: local name 'parser' is not defined in engine.main()")
    else:
        try:
            print(f"DEBUG: local name 'parser' -> type={type(_parser_val).__name__} repr={_parser_val!r}")
        except Exception:
            print(f"DEBUG: local name 'parser' -> type={type(_parser_val).__name__}")

    try:
        _arg_parser_val = arg_parser
    except NameError:
        print("DEBUG: local name 'arg_parser' is not defined in engine.main()")
    else:
        try:
            print(f"DEBUG: local name 'arg_parser' -> type={type(_arg_parser_val).__name__} repr={_arg_parser_val!r}")
        except Exception:
            print(f"DEBUG: local name 'arg_parser' -> type={type(_arg_parser_val).__name__}")

    args = arg_parser.parse_args(sys_argv)
    level = logging.INFO
    if args.verbose and args.quiet:
        raise AssertionError("Passed both --verbose and --quiet flags")
    if args.verbose:
        level = logging.DEBUG
    if args.quiet:
        level = logging.WARNING

    exclude_namespaces = list(filter(None, (args.exclude_namespaces or "").split(',')))
    exclude_functions = list(filter(None, (args.exclude_functions or "").split(',')))
    include_only_namespaces = list(filter(None, (args.include_only_namespaces or "").split(',')))
    include_only_functions = list(filter(None, (args.include_only_functions or "").split(',')))

    lang_params = LanguageParams(args.source_type, args.ruby_version)
    subset_params = SubsetParams.generate(args.target_function, args.upstream_depth,
                                          args.downstream_depth)

    alias_labels = bool(getattr(args, 'alias_labels', False))

    # If the user passed an explicit level via key=value on the command line,
    # prefer that over --verbose/--quiet computed value.
    if explicit_level is not None:
        level = explicit_level

    code2flow(
        raw_source_paths=args.sources,
        output_file=args.output,
        language=args.language,
        hide_legend=args.hide_legend,
        exclude_namespaces=exclude_namespaces,
        exclude_functions=exclude_functions,
        include_only_namespaces=include_only_namespaces,
        include_only_functions=include_only_functions,
        no_grouping=args.no_grouping,
        no_trimming=args.no_trimming,
        skip_parse_errors=args.skip_parse_errors,
        lang_params=lang_params,
        subset_params=subset_params,
        alias_labels=alias_labels,
        level=level,
        heuristics=args.heuristics,
        show_libraries=args.show_libraries,
    )
