# from https://ruby-doc.com/docs/ProgrammingRuby/html/tut_modules.html


class BaseScales():
    def baseNum(self):
        return self.numNotes
    

class MajorScales(BaseScales):
    def majorNum(self):
        self.baseNum()
        if self.numNotes is None:
            self.numNotes = 7
        return self.numNotes


class PentatonicScales(BaseScales):
    def pentaNum(self):
        self.baseNum()
        if self.numNotes is None:
            self.numNotes = 5
        return self.numNotes

