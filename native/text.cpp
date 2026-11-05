// CPython Unicode predicates preserve re.compile(r"\w+") token boundaries.
#include <pybind11/pybind11.h>
namespace py = pybind11;

static bool word(Py_UCS4 c) {
    return c == '_' || Py_UNICODE_ISALNUM(c);
}

static Py_ssize_t count_words(py::str text) {
    PyObject *s = text.ptr();
    const int kind = PyUnicode_KIND(s);
    const void *data = PyUnicode_DATA(s);
    const Py_ssize_t size = PyUnicode_GET_LENGTH(s);
    Py_ssize_t count = 0;
    bool inside = false;
    for (Py_ssize_t i = 0; i < size; ++i) {
        bool current = word(PyUnicode_READ(kind, data, i));
        if (current && !inside) ++count;
        inside = current;
    }
    return count;
}

static Py_ssize_t count_stopwords(py::str text, py::frozenset words) {
    // Caller performs Turkish-aware lowercasing using the existing Python code.
    PyObject *s = text.ptr();
    const int kind = PyUnicode_KIND(s);
    const void *data = PyUnicode_DATA(s);
    const Py_ssize_t size = PyUnicode_GET_LENGTH(s);
    Py_ssize_t count = 0, i = 0;
    while (i < size) {
        if (!word(PyUnicode_READ(kind, data, i))) { ++i; continue; }
        const Py_ssize_t start = i++;
        while (i < size && word(PyUnicode_READ(kind, data, i))) ++i;
        py::object token = py::reinterpret_steal<py::object>(PyUnicode_Substring(s, start, i));
        if (!token) throw py::error_already_set();
        const int found = PySet_Contains(words.ptr(), token.ptr());
        if (found < 0) throw py::error_already_set();
        count += found;
    }
    return count;
}

PYBIND11_MODULE(_news_native, m) {
    m.doc() = "Optional CPython Unicode token counters for news extraction";
    m.def("word_count", &count_words);
    m.def("stopword_count", &count_stopwords);
}
