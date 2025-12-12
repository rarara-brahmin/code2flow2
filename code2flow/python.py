import ast
import logging
import os

from .model import (OWNER_CONST, GROUP_TYPE, Group, Node, Call, Variable,
                    BaseLanguage, djoin)


def get_call_from_func_element(func, scope_stack=None):
    """
    Given a python ast that represents a function call, clear and create our
    generic Call object. Some calls have no chance at resolution (e.g. array[2](param))
    so we return nothing instead.

    :param func ast:
    :rtype: Call|None
    """
    assert type(func) in (ast.Attribute, ast.Name, ast.Subscript, ast.Call)
    if type(func) == ast.Attribute:
        # 型推論によるowner_tokenの解決
        owner_token = OWNER_CONST.UNKNOWN_VAR
        if scope_stack is not None and isinstance(func.value, ast.Name):
            var_name = func.value.id
            for scope in reversed(scope_stack):
                if var_name in scope:
                    owner_token = scope[var_name]
                    break
        else:
            # 従来のowner_token構築ロジック
            owner_token_list = []
            val = func.value
            while True:
                try:
                    val_attr = getattr(val, 'attr', val.id)
                    owner_token_list.append(val_attr)
                except AttributeError:
                    pass
                val = getattr(val, 'value', None)
                if not val:
                    break
            if owner_token_list:
                owner_token = djoin(*reversed(owner_token_list))

        func_value = getattr(func, "value", None)
        is_library = False
        if owner_token and owner_token != OWNER_CONST.UNKNOWN_VAR:
            is_library = True
        return Call(token=func.attr, line_number=func.lineno, owner_token=owner_token, is_library=is_library)
    if type(func) == ast.Name:
        return Call(token=func.id, line_number=func.lineno)
    if type(func) in (ast.Subscript, ast.Call):
        return None


def make_calls(lines, scope_stack=None):
    """
    Given a list of lines, find all calls in this list.

    :param lines list[ast]:
    :rtype: list[Call]
    """

    # ToDo: ast.Import, ast.ImprotFromの場合にcallに何を詰めて返すのか。
    calls = []
    for tree in lines:
        for element in ast.walk(tree):
            if type(element) != ast.Call:
                continue
            call = get_call_from_func_element(element.func, scope_stack)
            if call:
                calls.append(call)
    return calls


def process_assign(element, scope_stack=None):
    """
    Given an element from the ast which is an assignment statement, return a
    Variable that points_to the type of object being assigned. For now, the
    points_to is a string but that is resolved later.

    :param element ast:
    :rtype: Variable
    """

    if type(element.value) != ast.Call:
        return []
    call = get_call_from_func_element(element.value.func, scope_stack)
    if not call:
        return []

    ret = []
    # 型推論: 右辺がクラスインスタンス化なら型情報を記録
    class_name = None
    if isinstance(element.value.func, ast.Name):
        class_name = element.value.func.id
    for target in element.targets:
        if type(target) != ast.Name:
            continue
        token = target.id
        ret.append(Variable(token, call, element.lineno))
        # スコープスタックに型情報を記録
        if scope_stack is not None and class_name:
            scope_stack[-1][token] = class_name
    return ret


def process_import(element, scope_stack=None):
    """
    Given an element from the ast which is an import statement, return a
    Variable that points_to the module being imported. For now, the
    points_to is a string but that is resolved later.

    :param element ast:
    :rtype: Variable
    """
    ret = []

    for single_import in element.names:
        assert isinstance(single_import, ast.alias)
        token = single_import.asname or single_import.name
        rhs = single_import.name

        # For ImportFrom, element.module contains the package/module
        if hasattr(element, 'module') and element.module:
            rhs = djoin(element.module, rhs)

        # Record import mapping in current scope if provided
        if scope_stack is not None:
            scope_stack[-1][token] = rhs

        ret.append(Variable(token, points_to=rhs, line_number=element.lineno))
    return ret


def make_local_variables(lines, parent, scope_stack=None):
    """
    Given an ast of all the lines in a function, generate a list of
    variables in that function. Variables are tokens and what they link to.
    In this case, what it links to is just a string. However, that is resolved
    later.

    :param lines list[ast]:
    :param parent Group:
    :rtype: list[Variable]
    """
    variables = []
    if scope_stack is None:
        scope_stack = [{}]  # グローバルスコープ
    for tree in lines:
        for element in ast.walk(tree):
            if type(element) == ast.Assign:
                variables += process_assign(element, scope_stack)
            if type(element) in (ast.Import, ast.ImportFrom):
                variables += process_import(element, scope_stack)
    if parent.group_type == GROUP_TYPE.CLASS:
        variables.append(Variable('self', parent, lines[0].lineno))

    variables = list(filter(None, variables))
    # Trueとなるオブジェクトのみを取り出して再リスト化

    return variables


def get_inherits(tree):
    """
    Get what superclasses this class inherits
    This handles exact names like 'MyClass' but skips things like 'cls' and 'mod.MyClass'
    Resolving those would be difficult
    :param tree ast:
    :rtype: list[str]
    """
    return [base.id for base in tree.bases if type(base) == ast.Name]


class Python(BaseLanguage):
    @staticmethod
    def assert_dependencies():
        # ToDo: 実装されてないけど何？
        pass

    @staticmethod
    def get_tree(filename, _) -> ast.AST:
        """
        Get the entire AST for this file

        :param filename str:
        :rtype: ast
        """
        try:
            with open(filename) as f:
                raw = f.read()
        except ValueError:
            with open(filename, encoding='UTF-8') as f:
                raw = f.read()

        parsed_ast = ast.parse(raw)
        return parsed_ast

    @staticmethod
    def separate_namespaces(tree):
        """
        Given an AST, recursively separate that AST into lists of ASTs for the
        subgroups, nodes, and body. This is an intermediate step to allow for
        cleaner processing downstream

        :param tree ast:
        :returns: tuple of group, node, and body trees. These are processed
                  downstream into real Groups and Nodes.
        :rtype: (list[ast], list[ast], list[ast])
        """

        # ToDo: 最上位階層の呼び出し時にtree.body[4].body[1].targets[0].idにreqが格納されている。
        #   reqではなくrequest.getをnode_trees内にNodeとして登録したい。
        #   ⇒この時点では呼び先のノードを登録しているだけであって、呼び元の解析まで行っているわけではない？
        # ToDo: では呼び元の解析まで行って、呼び先と繋げているのはどの処理か？
        groups = []
        nodes: list[Node] = []
        body = []
        imports = []

        for el in tree.body:
            if type(el) in (ast.FunctionDef, ast.AsyncFunctionDef):
                nodes.append(el)
            elif type(el) == ast.ClassDef:
                groups.append(el)
            elif type(el) == ast.Import:
                imports.append(el)
            elif getattr(el, 'body', None):
                tup = Python.separate_namespaces(el)
                groups += tup[0]
                nodes += tup[1]
                body += tup[2]
            else:
                body.append(el)
        return groups, nodes, body, imports

    @staticmethod
    def make_nodes(tree: ast.AST, parent: Group) -> list[Node]:
        """
        node_treeからノードを取り出してリスト化する。
        Given an ast of all the lines in a function, create the node along with the
        calls and variables internal to it.

        :param tree ast:
        :param parent Group:
        :rtype: list[Node]
        """
        token = tree.name
        line_number = tree.lineno
        # スコープスタックを関数ごとにpush/pop
        # Initialize function scope with module-level imports if available so
        # that references to module names (e.g. `re`) resolve inside functions.
        module_scope = {}
        if getattr(parent, 'group_type', None) == GROUP_TYPE.FILE:
            module_scope = getattr(parent, 'module_scope', {}) or {}
        elif getattr(parent, 'group_type', None) == GROUP_TYPE.CLASS:
            module_scope = getattr(parent.parent, 'module_scope', {}) or {}
        # copy to avoid mutating the shared module scope
        scope_stack = [dict(module_scope), {}]
        variables = make_local_variables(tree.body, parent, scope_stack)
        calls = make_calls(tree.body, scope_stack)
        is_constructor = False
        if parent.group_type == GROUP_TYPE.CLASS and token in ['__init__', '__new__']:
            is_constructor = True

        import_tokens = []
        if parent.group_type == GROUP_TYPE.FILE:
            import_tokens = [djoin(parent.token, token)]

        ret = [Node(token, calls, variables, parent, import_tokens=import_tokens,
                    line_number=line_number, is_constructor=is_constructor)]
        # ToDo: この時点では呼び先の外部モジュールは[Node.calls[n].owner_token]に格納されている。
        #       呼ばれる側の外部モジュールのNodeを作ってやればグラフ化できるはず。
        return ret

    @staticmethod
    def make_import_module_nodes(import_module: ast.Import, parent):
        """
        ToDo: make_nodesメソッドをベースにしてimportモジュールをノード化したい。
            import_treeからimportモジュールをnode化するようにmake_nodesを改造する。

        Todo: import_treeはast.Import（ast.FunctionDefとの構造の違いに注意）
        なのでこれをdot言語に起こせるように成形してNodeに詰める。
        dot言語作成時に読む属性はlabel, name, shape, style, fillcolor
        （詳細はmodel.py Node.to_dot()参照）
        :param tree ast.Import:
        :param parent Group:
        :rtype: list[Node]
        """

        # ToDo: Node.variableを詰める必要あり
        name = import_module.names[0].name
        token = name
        line_number = import_module.lineno
        # ToDo: 多分これではだめ。import a, b, cみたいな場合に対応できない。

        token = name
        line_number = import_module.lineno
        # calls = make_calls(tree.body) # いらない？
        # variables = make_local_variables(tree.body, parent)
        # is_constructor = False
        # if parent.group_type == GROUP_TYPE.CLASS and token in ['__init__', '__new__']:
        #     is_constructor = True

        import_tokens = []
        if parent.group_type == GROUP_TYPE.FILE:
            import_tokens = [djoin(parent.token, token)]

        ret = [Node(token, [], None, parent, import_tokens=import_tokens,
                    line_number=line_number, is_constructor=False, is_library=True)]

        return ret

    @staticmethod
    def make_root_node(lines, parent):
        """
        The "root_node" is an implict node of lines which are executed in the global
        scope on the file itself and not otherwise part of any function.

        :param lines list[ast]:
        :param parent Group:
        :rtype: Node
        """
        token = "(global)"
        line_number = 0
        # Create a scope for module-level (global) variables/imports
        scope_stack = [{}]
        # Populate imports/assignments into the scope before extracting calls
        # so that calls like `re.match()` can resolve `re` from the import.
        variables = make_local_variables(lines, parent, scope_stack)
        # Expose module-level scope on the parent group so functions can inherit it
        try:
            parent.module_scope = scope_stack[0]
        except Exception:
            pass
        calls = make_calls(lines, scope_stack)
        return Node(token, calls, variables, line_number=line_number, parent=parent)

    @staticmethod
    def make_class_group(tree, parent):
        """
        Given an AST for the subgroup (a class), generate that subgroup.
        In this function, we will also need to generate all of the nodes internal
        to the group.

        :param tree ast:
        :param parent Group:
        :rtype: Group
        """
        assert type(tree) == ast.ClassDef
        subgroup_trees, node_trees, body_trees, import_trees = Python.separate_namespaces(tree)

        group_type = GROUP_TYPE.CLASS
        token = tree.name
        display_name = 'Class'
        line_number = tree.lineno

        import_tokens = [djoin(parent.token, token)]
        inherits = get_inherits(tree)

        class_group = Group(token, group_type, display_name, import_tokens=import_tokens,
                            inherits=inherits, line_number=line_number, parent=parent)

        for node_tree in node_trees:
            class_group.add_node(Python.make_nodes(node_tree, parent=class_group)[0])

        for subgroup_tree in subgroup_trees:
            logging.warning("Code2flow does not support nested classes. Skipping %r in %r.",
                            subgroup_tree.name, parent.token)
        return class_group

    @staticmethod
    def file_import_tokens(filename):
        """
        Returns the token(s) we would use if importing this file from another.

        :param filename str:
        :rtype: list[str]
        """
        return [os.path.split(filename)[-1].rsplit('.py', 1)[0]]
