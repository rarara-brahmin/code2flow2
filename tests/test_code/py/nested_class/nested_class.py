class Outer():
    class Inner():
        def inner_func(self):
            Outer().outer_func()

    @staticmethod
    def outer_func(self, a):
        print("Outer_func")
        a.inner_func()

    def __init__(self):
        self.inner = self.Inner()
        print("do something")


new_obj = Outer()
inr_obj = new_obj.Inner()
new_obj.outer_func(inr_obj)
