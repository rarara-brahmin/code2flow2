# Node
| Name        | Source                                                                                                                                                                                                                                                                                                                                                            | type   |
|:------------|:------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:-------|
| token       | tree.name<br/> ⇒Python.separate_namespaces()で取得したnode_treesの中身<br/>⇒ASTのbodyの要素のtypeがast.FunctionDef, ast.AsyncFunctionDefの場合にその要素を登録                                                                                                                                                                                                                             |        |
| line_number | tree.lineno<br/> ⇒Python.separate_namespaces()で取得したnode_treesの中身<br/>⇒ASTのbodyの要素のtypeがast.FunctionDef, ast.AsyncFunctionDefの場合にその要素を登録                                                                                                                                                                                                                           |        |
| calls | tree.bodyを走査(ast.walk)して要素内にast.Callがある場合に、そのelementのfunc(element.func)を取り出す。<br> element.funcがast.Attributeの場合にelement.func.valを取り出し、そのvalに"value"という名前の属性があった場合にそれを取り出す。                                                                                                                                                                                        |        |
| variables | make_local_variables()で作成<br> tree.bodyを走査(ast.walk)して<br><br/> 要素内にast.Assginがある場合に、そのelement.valueがast.Callでない場合、そのelement.targetsからast.Name以外の要素を取り出してVariableのリストを生成。Variableの詳細はVariableを参照。<br><br> 要素内にast.Import, ast.ImportFromがある場合にImport.names[].asname or nameをtoken, points_toをImport.names[].name, line_numberをImport.linenoとしてVariableのリストを生成して格納 |        |
| parent | make_nodes()の入力になったclass_group                                                                                                                                                                                                                                                                                                                                    |        |
| import_tokens | parent(=class_group).tokenとtoken(tree.name)を"."で結合                                                                                                                                                                                                                                                                                                                |        |
| is_constructor | make_nodes()の入力になったparentのgroup_typeがGROUP_TYPE.CLASSかつtokenが__init__もしくは__new__の時にTrue                                                                                                                                                                                                                                                                           | bool   |
| is_library | 自作                                                                                                                                                                                                                                                                                                                                                                | bool   |
| uid | node_[ユニークな整数(16進4桁)]                                                                                                                                                                                                                                                                                                                                             | string |
| is_leaf | デフォルトTrue（Nodeクラスコンストラクタ参照）<br> Edgeクラスのコンストラクタで、コンストラクタの入力になっているnodeのis_leafとis_trunkを切り替えている。                                                                                                                                                                                                                                                                   | bool |
| is_trunk | デフォルトTrue（Nodeクラスコンストラクタ参照）<br>                                                                                                                                                                                                                                                                                                                                   | bool |

# Call
| Name        | Source                                                                                                                                | type |
|:------------|:--------------------------------------------------------------------------------------------------------------------------------------|:-----|
| token       | tree.name<br/> ⇒Python.separate_namespaces()で取得したnode_treesの中身<br/>⇒ASTのbodyの要素のtypeがast.FunctionDef, ast.AsyncFunctionDefの場合にその要素を登録 |      |
| owner_token | get_call_from_func_element()の引数funcのvalue属性にattrという属性がある場合その名前を"."で繋げて文字列として格納。ない場合は"UNKNOWN_VAR"が入る。                                 |      |
| line_number | get_call_from_func_element()の引数funcのlineno属性                                                                                          |      |
| definite_constructor |                                                                                                                                       |
| is_library |                                                                                                                                       |      |
生成器: process_assign() or make_calls() -> get_call_from_func_element()<br>
Callインスタンスはfuncがast.Attribute型、ast.Name型の時に生成される。ast.Subscript型、ast.Call型の時には生成されない。

# Variable
| Name        | Source                                                               | type   |
|:------------|:---------------------------------------------------------------------|:-------|
| token       | target.id<br> element.targets[].id                                   |
| points_to   | element.value.funcからCallを作成して詰める。<br> get_call_from_func_element()参照 | |
| line_number | element.lineno | |                                                      

# Edge
| Name  | Source  | type   |
|:------|:--------|:-------|
| node0 | 呼び元のノード |  |
| node1 | 呼び先のノード |  |
Edge.to_dot()で"node0.uid -> node1.uid"を返す。

# Group
| Name          | Source                                                                                                      | type   |
|:--------------|:------------------------------------------------------------------------------------------------------------|:-------|
| token         | file_groupの場合: ファイル名                                                                                        |  |
| line_number   | fileg_roupの場合: 0固定<br> class_groupの場合: make_class_groupの入力のtreeのlineno属性                                    |  |
| nodes         |                                                                                                             |  |
| root_node     |                                                                                                             |  |
| subgroups     |                                                                                                             |  |
| parent        |                                                                                                             |  |
| group_type    | file_groupの場合: GROUP_TYPE.FILE                                                                              |  |
| display_type  | file_groupの場合: 'File'                                                                                       |  |
| import_tokens | file_groupの場合: language.file_import_tokens(filename)の出力<br> make_class_groupの入力parentのtoken属性と入力treeのname属性 |  |
| inherits      | class_groupの場合: make_class_groupの入力treeのbases属性のうちast.Name型の要素のid属性をリストにして格納                                |  |
| imports |                                                                                                             |  |
| uid | cluster_[ユニークな整数(16進4桁)]                                                                                    |  |
生成器:<br>
map_it() -> make_file_group()
map_it() -> make_file_group() -> Python.make_class_group()




