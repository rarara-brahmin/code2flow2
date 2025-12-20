import ast
import logging
import os

from .model import (OWNER_CONST, GROUP_TYPE, Group, Node, Call, Variable,
                    BaseLanguage, djoin)


def get_call_from_func_element(func, scope_stack=None, call_node=None):
    """
    Given a python ast that represents a function call, clear and create our
    generic Call object. Some calls have no chance at resolution (e.g. array[2](param))
    so we return nothing instead.

    :param func ast:
    :rtype: Call|None
    """
    assert type(func) in (ast.Attribute, ast.Name, ast.Subscript, ast.Call)
    # Collect positional argument tokens if a full ast.Call node was provided
    arg_tokens = []
    if call_node is not None:
        for a in getattr(call_node, 'args', []):
            if isinstance(a, ast.Name):
                arg_tokens.append(a.id)
            elif isinstance(a, ast.Attribute):
                # build dotted attribute name
                parts = []
                val = a
                while True:
                    try:
                        parts.append(getattr(val, 'attr', val.id))
                    except AttributeError:
                        pass
                    val = getattr(val, 'value', None)
                    if not val:
                        break
                if parts:
                    arg_tokens.append(djoin(*reversed(parts)))

    if type(func) == ast.Attribute:
        # 型推論によるowner_tokenの解決
        owner_token = OWNER_CONST.UNKNOWN_VAR
        if scope_stack is not None and isinstance(func.value, ast.Name):
            var_name = func.value.id
            for scope in reversed(scope_stack):
                if var_name in scope:
                    val = scope[var_name]
                    # If the scope mapping stores a string, treat it as a module/type
                    # name and use that. If it's a non-string (e.g. Group/Node), or
                    # we want variable-based resolution, use the variable name so
                    # later matching against variable.token works.
                    if isinstance(val, str):
                        owner_token = val
                    else:
                        owner_token = var_name
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
        return Call(token=func.attr, line_number=func.lineno, owner_token=owner_token, is_library=is_library, arg_tokens=arg_tokens)
    if type(func) == ast.Name:
        # If the name is an imported symbol that maps to a dotted module
        # path recorded in the scope (e.g. `search -> 're.search'`), treat
        # this as an attribute call so resolution can find the library
        # node (owner_token='re', token='search'). This handles both
        # `from re import search` and `import re.match as mitch` patterns.
        if scope_stack is not None:
            for scope in reversed(scope_stack):
                if func.id in scope:
                    val = scope[func.id]
                    if isinstance(val, str) and '.' in val:
                        module, attr = val.rsplit('.', 1)
                        return Call(token=attr, line_number=func.lineno,
                                    owner_token=module, is_library=True,
                                    arg_tokens=arg_tokens)
                    # If the mapping isn't a dotted string, fall through
                    break
        return Call(token=func.id, line_number=func.lineno, arg_tokens=arg_tokens)
    # Handle subscript calls like `func_dict['name']()` by resolving the
    # subscript against scope_stack mappings created by `process_assign`.
    if type(func) == ast.Subscript:
        # Only handle simple cases: Name[...] where slice is a constant string
        val = getattr(func, 'value', None)
        slc = getattr(func, 'slice', None)
        key = None
        if isinstance(slc, ast.Constant):
            key = slc.value
        else:
            # Python <3.9 used ast.Index
            try:
                if isinstance(slc, ast.Index) and isinstance(slc.value, ast.Constant):
                    key = slc.value.value
            except Exception:
                key = None
        if isinstance(val, ast.Name) and isinstance(key, str) and scope_stack is not None:
            name = val.id
            # Look up mapping in scope stack
            for scope in reversed(scope_stack):
                if name in scope:
                    mapping = scope[name]
                    # mapping expected to be a dict of str->str created in process_assign
                    if isinstance(mapping, dict) and key in mapping:
                        target_name = mapping[key]
                        c = Call(token=target_name, line_number=func.lineno, arg_tokens=arg_tokens)
                        # annotate for labeling later
                        try:
                            c.indirect = ('dict', name, key)
                        except Exception:
                            pass
                        return c
        return None
    # Handle nested calls like `factory()(5)` where func is itself a Call.
    if type(func) == ast.Call:
        # Get the inner call (the function producing a callable)
        inner = get_call_from_func_element(func.func, scope_stack, call_node=func.func)
        if inner:
            # Produce a Call that keeps reference to the inner call so
            # resolution can inspect the producer's return info later.
            c = Call(token=inner.token, line_number=func.lineno, arg_tokens=arg_tokens)
            try:
                c.factory_call = inner.token
            except Exception:
                pass
            return c
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
            call = get_call_from_func_element(element.func, scope_stack, call_node=element)
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

    # Handle calls: obj = SomeClass() or obj = factory()
    if type(element.value) == ast.Call:
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

    # Handle dict literal assignments like func_dict = {'func_a': func_a, ...}
    if type(element.value) == ast.Dict and scope_stack is not None:
        mapping = {}
        keys = getattr(element.value, 'keys', [])
        values = getattr(element.value, 'values', [])
        for k, v in zip(keys, values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Name):
                mapping[k.value] = v.id
        ret = []
        for target in element.targets:
            if type(target) != ast.Name:
                continue
            token = target.id
            # Record mapping in current scope so get_call_from_func_element can use it
            scope_stack[-1][token] = mapping
            # Also create a Variable that points to this mapping for debugging
            ret.append(Variable(token, mapping, element.lineno))
        return ret

    return []


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
    # Initialize scope_stack if not provided.
    if scope_stack is None:
        scope_stack = [dict()]  # グローバルスコープ
    for tree in lines:
        for element in ast.walk(tree):
            if type(element) == ast.Assign:
                variables += process_assign(element, scope_stack)
            if type(element) in (ast.Import, ast.ImportFrom):
                variables += process_import(element, scope_stack)
    if parent.group_type == GROUP_TYPE.CLASS:
        variables.append(Variable('self', parent, lines[0].lineno))
        # Ensure 'self' is present in the scope mapping so attribute calls
        # like self.foo() can have owner_token resolved as the variable name
        # (handled in get_call_from_func_element).
        # Ensure 'self' exists in the scope for method resolution when parsing
        # inside class methods. This allows calls like `self.foo()` to be
        # resolved via variable-based matching.
        scope_stack[-1].setdefault('self', 'self')

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
        # Include function parameters as variables so they can be resolved/propagated later
        param_tokens = [arg.arg for arg in getattr(tree.args, 'args', [])]
        param_vars = [Variable(p, OWNER_CONST.UNKNOWN_VAR, line_number) for p in param_tokens]
        variables = param_vars + make_local_variables(tree.body, parent, scope_stack)
        calls = make_calls(tree.body, scope_stack)
        is_constructor = False
        if parent.group_type == GROUP_TYPE.CLASS and token in ['__init__', '__new__']:
            is_constructor = True

        import_tokens = []
        if parent.group_type == GROUP_TYPE.FILE:
            import_tokens = [djoin(parent.token, token)]

        node = Node(token, calls, variables, parent, import_tokens=import_tokens,
                    line_number=line_number, is_constructor=is_constructor)
        # Record simple return info for factory detection: names returned
        return_tokens = []
        returns_lambda = False
        for el in ast.walk(tree):
            if isinstance(el, ast.Return):
                val = getattr(el, 'value', None)
                if isinstance(val, ast.Name):
                    return_tokens.append(val.id)
                elif isinstance(val, ast.Lambda):
                    returns_lambda = True
        node.return_tokens = return_tokens
        node.returns_lambda = returns_lambda
        # record parameter ordering for argument-propagation heuristic
        node.param_tokens = param_tokens
        ret = [node]
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
            # Create nested class groups instead of skipping them. Pass the
            # current class_group as the parent so nesting is preserved.
            class_group.add_subgroup(Python.make_class_group(subgroup_tree, parent=class_group))
        return class_group

    @staticmethod
    def file_import_tokens(filename):
        """
        Returns the token(s) we would use if importing this file from another.

        :param filename str:
        :rtype: list[str]
        """
        return [os.path.split(filename)[-1].rsplit('.py', 1)[0]]
