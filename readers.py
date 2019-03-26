#! /usr/bin/env python3
"""
A series of functions used to read different file types.

"""
import base64
import collections
import csv
import re
import struct
import zlib

import lxml.etree as etree

from constants import AA_SYMBOLS, ELEMENT_MASSES, MassType
import mass_spectrum


Modification = collections.namedtuple("Modification", ["name", "mono"])
PPRes = collections.namedtuple("PPRes", ["seq", "mods", "theor_z", "spec",
                                         "time", "conf", "theor_mz", "prec_mz",
                                         "accs", "names"])

MGF_TITLE_REGEX = re.compile(r"TITLE=Locus:([\d\.]+) ")


class ParserException(Exception):
    """
    A custom exception to be raised during file parse errors.

    """


def _strip_line(string, nchars=2):
    """
    Strips trailing whitespace, preceeding nchars and preceeding whitespace
    from the given string.

    Args:
        string (str): The string to strip.
        nchars (int, optional): The number of preceeding characters to remove.

    Returns:
        The stripped string.

    """
    return string.rstrip()[nchars:].lstrip()


def read_uniprot_ptms(ptm_file):
    """
    Parses the PTM list provided by the UniProt Knowledgebase
    https://www.uniprot.org/docs/ptmlist.

    Args:
        ptm_file (str): The path to the PTM list file.

    Returns:
        A dictionary mapping residues to Modifications.

    """
    ptms = collections.defaultdict(list)
    with open(ptm_file) as fh:
        # Read the file until the entries begin at a line of underscores
        line = next(fh)
        while not line.startswith("______"):
            line = next(fh)
        mod = {}
        for line in fh:
            if line.startswith("ID"):
                mod["name"] = _strip_line(line)
            elif line.startswith("TG"):
                if "Undefined" in line:
                    continue
                res_str = _strip_line(line)
                res_name = (res_str.split('-')[0] if '-' in res_str
                            else res_str[:-1])
                mod["res"] = [AA_SYMBOLS[r] for r in res_name.split(" or ")
                              if r in AA_SYMBOLS]
            elif line.startswith("MM"):
                mod["mass"] = float(_strip_line(line))
            elif line.startswith("//"):
                for res in mod.get("res", []):
                    ptms[res].append(Modification(mod["name"],
                                                  mod.get("mass", None)))
                mod = {}
    return ptms


MOD_FORMULA_REGEX = re.compile(r"(\w+)\(([0-9]+)\)")

def parse_mod_formula(formula, mass_type):
    """
    Parses the given modification chemical formula to determine the
    associated mass change.

    Args:
        formula (str): The modification chemical formula.
        mass_type (MassType): The mass type to calculate.

    Returns:
        The mass of the modification as a float.

    """
    return sum([getattr(ELEMENT_MASSES[e], mass_type.name) * int(c)
                for e, c in MOD_FORMULA_REGEX.findall(formula)])


class PTMDB():
    """
    A class representing the UniMod PTM DB data structure.

    """
    _mass_keys = ['Monoisotopic mass', 'Average mass']
    _name_keys = ['PSI-MS Name', 'Interim name']
    _desc_key = 'Description'

    def __init__(self, ptm_file):
        """
        Initializes the class by setting up the composed dictionary.

        Args:
            ptm_file (str): The path to the UniMod PTM file.

        """
        self._data = {
            'Monoisotopic mass': [],
            'Average mass': [],
            # Each of the below keys store a dictionary mapping their position in
            # the above mass lists
            'PSI-MS Name': {},
            'Interim name': {},
            'Description': {}
        }

        with open(ptm_file, newline='') as fh:
            reader = csv.DictReader(fh, delimiter='\t')
            for row in reader:
                self.add_entry(row)

    def add_entry(self, entry):
        """
        Adds a new entry to the database.

        Args:
            entry (dict): A row from the UniMod PTB file.

        """
        pos = len(self._data[PTMDB._mass_keys[0]])
        for key in PTMDB._mass_keys:
            self._data[key].append(float(entry[key]))
        for key in PTMDB._name_keys:
            self._data[key][entry[key]] = pos
        self._data[PTMDB._desc_key][entry[key].replace(' ', '').lower()] = pos

    def get_mass(self, name, mass_type=MassType.mono):
        """
        Retrieves the mass of the specified modification.

        Args:
            name (str): The name of the modification.
            mass_type (MassType, optional): The type of mass to retrieve.

        Returns:
            The mass as a float or None.

        """
        mass_key = (PTMDB._mass_keys[0] if mass_type is MassType.mono
                    else PTMDB._mass_keys[1])
        # Try matching either of the two name fields, using PSI-MS Name first
        for key in PTMDB._name_keys:
            idx = self._data[key].get(name, None)
            if idx is not None:
                return self._data[mass_key][idx]

        # Try matching the description
        name = name.replace(' ', '')
        if name.lower().startswith("delta"):
            return parse_mod_formula(name, mass_type)
        idx = self._data[PTMDB._desc_key].get(name.lower(), None)
        return None if idx is None else self._data[mass_key][idx]


def _build_ppres(row):
    """
    Processes the given row of a Peptide Summary file to produce a PPRes
    entry.

    Args:
        row (dict): A row dictionary from the Peptide Summary file.

    Returns:
        A PPRes namedtuple to represent the row.

    """
    return PPRes(row["Sequence"], row["Modifications"], int(row["Theor z"]),
                 row["Spectrum"], row["Time"], float(row["Conf"]),
                 float(row["Theor m/z"]), float(row["Prec m/z"]),
                 row["Accessions"], row["Names"])


def read_peptide_summary(summary_file, condition=None):
    """
    Reads the given ProteinPilot Peptide Summary file to extract useful
    information on sequence, modifications, m/z etc.

    Args:
        summary_file (str): The path to the Peptide Summary file.
        condition (func, optional): A boolean-returning function which
                                    determines whether a row should be
                                    returned.

    Returns:
        The read information as a list of PPRes NamedTuples.

    """
    with open(summary_file, newline='') as fh:
        reader = csv.DictReader(fh, delimiter='\t')
        return ([_build_ppres(r) for r in reader] if condition is None
                else [_build_ppres(r) for r in reader if condition(r)])


def read_mgf_file(spec_file):
    """
    Reads the given tandem mass spectrometry data file to extract individual
    spectra.

    Args:
        spec_file (str): The path to the MGF file to read.

    Returns:
        A dictionary of spectrum ID to numpy array of peaks.

    """
    spectra = {}
    spec_id = None
    with open(spec_file) as fh:
        peaks, mz, charge = [], None, None
        for line in fh:
            if line.startswith("END IONS"):
                if spec_id is None:
                    raise ParserException(
                        f"No spectrum ID found in MGF block in {spec_file}")
                spectra[spec_id] = mass_spectrum.Spectrum(peaks, float(mz),
                                                          charge)
                peaks, spec_id, mz, charge = [], None, None, None
            elif line.startswith('TITLE'):
                spec_id = MGF_TITLE_REGEX.match(line).group(1)
            elif line.startswith("PEPMASS"):
                mz = line.rstrip().split("=")[1]
            elif line.startswith("CHARGE"):
                charge = int(line.rstrip().rstrip("+").split("=")[1])
            elif '=' not in line and not line.startswith('BEGIN IONS'):
                peaks.append([float(n) for n in line.split()[:2]])

    return spectra


def read_mzml_file(spec_file):
    """
    Reads the given mzML file to extract spectra.

    """
    # TODO
    raise NotImplementedError()


def read_mzxml_file(spec_file):
    """
    Reads the given mzXML file to extract spectra.

    """
    # TODO
    raise NotImplementedError()


def read_spectra_file(spec_file):
    """
    Determines the format of the given tandem mass spectrum file and delegates
    to the appropriate reader.

    Args:
        spec_file (str): The path to the spectrum file to read.

    Returns:

    """
    if spec_file.endswith('.mgf'):
        return read_mgf_file(spec_file)
    if spec_file.lower().endswith('.mzml'):
        return read_mzml_file(spec_file)
    if spec_file.lower().endswith('.mzxml'):
        return read_mzxml_file(spec_file)
    raise NotImplementedError(
        f"Unsupported spectrum file type for {spec_file}")


def decodebinary(string, default_array_length, precision=64, bzlib='z'):
    """
    Decode binary string to float points.
    If provided, should take endian order into consideration.
    """
    decoded = base64.b64decode(string)
    decoded = zlib.decompress(decoded) if bzlib == 'z' else decoded
    unpack_format = "<%dd"%default_array_length if precision == 64 else \
        "<%dL"%default_array_length
    return struct.unpack(unpack_format, decoded)


def mzml_extract_ms1(mzml_file, namespace="http://psi.hupo.org/ms/mzml"):
    """
    Extracts the MS1 spectra from the input mzML file.

    Args:
        msml_file (str): The path to the mzML file.
        namespace (str, optional): The XML namespace used in the mzML file.

    Returns:
        A list of the MS1 spectra encoded in dictionaries.

    """
    spectra = []
    ns_map = {'x': namespace}
    # read from xml data
    for event, element in etree.iterparse(mzml_file, events=['end']):
        if event == 'end' and element.tag == f"{{{namespace}}}spectrum":
            # This contains the cycle and experiment information
            spectrum_info = dict(element.items())
            default_array_length = int(spectrum_info['default_array_length'])

            # MS level
            if element.find(f"{{{namespace}}}precursorList"):
                # Ignore MS level >= 2
                continue

            # MS spectrum
            mz_binary = element.xpath(
                "x:binaryDataArrayList/x:binaryDataArray"
                "[x:cvParam[@name='m/z array']]/x:binary",
                namespaces=ns_map)[0]
            int_binary = element.xpath(
                "x:binaryDataArrayList/x:binaryDataArray"
                "[x:cvParam[@name='intensity array']]/x:binary",
                namespaces=ns_map)[0]
            mz = decodebinary(mz_binary.text, default_array_length)
            intensity = decodebinary(int_binary.text, default_array_length)

            # Retention time
            start_time = float(element.xpath(
                "x:scanList/x:scan/x:cvParam[@name='scan start time']",
                namespaces=ns_map)[0].get("value"))

            element.clear()

            # Remove spectral peaks with intensity 0
            mz_intensity = [(mz[ii], intensity[ii])
                            for ii in range(default_array_length)
                            if intensity[ii] > 0]
            mz, intensity = zip(*mz_intensity)

            spectra.append({
                'mz': mz,
                'intensity': intensity,
                'rt': start_time,
                'info': spectrum_info
            })

    return spectra


def read_fasta_sequences(fasta_file):
    """
    Retrieves sequences from the input fasta_file.

    Args:
        fasta_file (TextIOWrapper): An open file handle to the fasta file.

    Yields:
        Sequences from the input file.

    """
    subseqs = []
    for line in fasta_file:
        if line.startswith('>'):
            if subseqs:
                yield title, ''.join(subseqs)
            title = line.rstrip()
            subseqs = []
        else:
            subseqs.append(line.rstrip())
    if subseqs:
        yield title, ''.join(subseqs)


if __name__ == '__main__':
    read_spectra_file('testdata\\I08\\I08_MGFPeaklist.mgf')
