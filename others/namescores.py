from nameparser import HumanName
from rapidfuzz import fuzz

def name_score(n1, n2):
    if not n1 or not n2:
        return 0.0

    hn1, hn2 = HumanName(n1.upper().strip()), HumanName(n2.upper().strip())
    fn1, ln1 = hn1.first, hn1.last
    fn2, ln2 = hn2.first, hn2.last

    first_score = fuzz.partial_ratio(fn1, fn2)
    last_score = fuzz.partial_ratio(ln1, ln2)

    return round((0.5 * first_score) + (0.5 * last_score), 2)

print(name_score("Vishwa Kumar", "Vishwa Jayabalan"))

#Vishwa J - Vishwa Jayabalan - 100
#Vishwa J - Vishwa Kumar - 40
#Vishwa Kumar - Vishwa Jayabalan - 57
#Vishwa J - Vishwa K - 40

#Vishwa - Vishwa J - 40 - To handle