#!/usr/bin/env python
# Copyright 2014-2018 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

from functools import reduce
import numpy
from pyscf import lib
from pyscf.lib import logger
from pyscf.ao2mo import _ao2mo
from pyscf.tdscf import uhf
from pyscf.scf import uhf_symm
from pyscf.pbc.tdscf.rhf import _get_eai
from pyscf.pbc.scf.newton_ah import _gen_uhf_response
from pyscf import __config__

REAL_EIG_THRESHOLD = getattr(__config__, 'pbc_tdscf_uhf_TDDFT_pick_eig_threshold', 1e-3)
POSTIVE_EIG_THRESHOLD = getattr(__config__, 'pbc_tdscf_uhf_TDDFT_positive_eig_threshold', 1e-3)

class TDA(uhf.TDA):

    conv_tol = getattr(__config__, 'pbc_tdscf_rhf_TDA_conv_tol', 1e-6)

    def __init__(self, mf):
        from pyscf.pbc import scf
        assert(isinstance(mf, scf.khf.KSCF))
        self.cell = mf.cell
        uhf.TDA.__init__(self, mf)

    def get_vind(self, mf):
        '''Compute Ax'''
        singlet = self.singlet
        cell = mf.cell
        kpts = mf.kpts

        mo_coeff = mf.mo_coeff
        mo_energy = mf.mo_energy
        mo_occ = mf.mo_occ
        nkpts = len(mo_occ)
        nao, nmo = mo_coeff[0][0].shape
        occidxa = [numpy.where(mo_occ[0][k]> 0)[0] for k in range(nkpts)]
        occidxb = [numpy.where(mo_occ[1][k]> 0)[0] for k in range(nkpts)]
        viridxa = [numpy.where(mo_occ[0][k]==0)[0] for k in range(nkpts)]
        viridxb = [numpy.where(mo_occ[1][k]==0)[0] for k in range(nkpts)]
        orboa = [mo_coeff[0][k][:,occidxa[k]] for k in range(nkpts)]
        orbob = [mo_coeff[1][k][:,occidxb[k]] for k in range(nkpts)]
        orbva = [mo_coeff[0][k][:,viridxa[k]] for k in range(nkpts)]
        orbvb = [mo_coeff[1][k][:,viridxb[k]] for k in range(nkpts)]

        e_ai_a = _get_eai(mo_energy[0], mo_occ[0])
        e_ai_b = _get_eai(mo_energy[1], mo_occ[1])
        hdiag = numpy.hstack([x.ravel() for x in (e_ai_a + e_ai_b)])
        tot_x_a = sum(x.size for x in e_ai_a)
        tot_x_b = sum(x.size for x in e_ai_b)

        mem_now = lib.current_memory()[0]
        max_memory = max(2000, self.max_memory*.8-mem_now)
        vresp = _gen_uhf_response(mf, hermi=0, max_memory=max_memory)

        def vind(zs):
            nz = len(zs)
            zs = [_unpack(z, mo_occ) for z in zs]
            dmvo = numpy.empty((2,nz,nkpts,nao,nao), dtype=numpy.complex128)
            for i in range(nz):
                dm1a, dm1b = zs[i]
                for k in range(nkpts):
                    dmvo[0,i,k] = reduce(numpy.dot, (orbva[k], dm1a[k], orboa[k].T.conj()))
                    dmvo[1,i,k] = reduce(numpy.dot, (orbvb[k], dm1b[k], orbob[k].T.conj()))

            v1ao = vresp(dmvo)
            v1s = []
            for i in range(nz):
                dm1a, dm1b = zs[i]
                v1as = []
                v1bs = []
                for k in range(nkpts):
                    v1a = reduce(numpy.dot, (orbva[k].T.conj(), v1ao[0,i,k], orboa[k]))
                    v1b = reduce(numpy.dot, (orbvb[k].T.conj(), v1ao[1,i,k], orbob[k]))
                    v1a += e_ai_a[k] * dm1a[k]
                    v1b += e_ai_b[k] * dm1b[k]
                    v1as.append(v1a.ravel())
                    v1bs.append(v1b.ravel())
                v1s += v1as + v1bs
            return numpy.hstack(v1s).reshape(nz,-1)

        return vind, hdiag

    def init_guess(self, mf, nstates=None):
        if nstates is None: nstates = self.nstates

        mo_energy = mf.mo_energy
        mo_occ = mf.mo_occ
        e_ai_a = _get_eai(mo_energy[0], mo_occ[0])
        e_ai_b = _get_eai(mo_energy[1], mo_occ[1])
        eai = numpy.hstack([x.ravel() for x in (e_ai_a + e_ai_b)])
        nov = eai.size
        nroot = min(nstates, nov)
        x0 = numpy.zeros((nroot, nov))
        idx = numpy.argsort(eai)
        for i in range(nroot):
            x0[i,idx[i]] = 1  # lowest excitations
        return x0

    def kernel(self, x0=None):
        '''TDA diagonalization solver
        '''
        self.check_sanity()
        self.dump_flags()

        vind, hdiag = self.get_vind(self._scf)
        precond = self.get_precond(hdiag)
        if x0 is None:
            x0 = self.init_guess(self._scf, self.nstates)

        self.converged, self.e, x1 = \
                lib.davidson1(vind, x0, precond,
                              tol=self.conv_tol,
                              nroots=self.nstates, lindep=self.lindep,
                              max_space=self.max_space,
                              verbose=self.verbose)

        mo_occ = self._scf.mo_occ
        tot_x_a = sum((occ>0).sum()*(occ==0).sum() for occ in mo_occ[0])
        self.xy = [(_unpack(xi, mo_occ),  # (X_alpha, X_beta)
                    (0, 0))  # (Y_alpha, Y_beta)
                   for xi in x1]
        #TODO: analyze CIS wfn point group symmetry
        return self.e, self.xy
CIS = TDA


class TDHF(TDA):
    def get_vind(self, mf):
        singlet = self.singlet
        cell = mf.cell
        kpts = mf.kpts

        mo_coeff = mf.mo_coeff
        mo_energy = mf.mo_energy
        mo_occ = mf.mo_occ
        nkpts = len(mo_occ)
        nao, nmo = mo_coeff[0][0].shape
        occidxa = [numpy.where(mo_occ[0][k]> 0)[0] for k in range(nkpts)]
        occidxb = [numpy.where(mo_occ[1][k]> 0)[0] for k in range(nkpts)]
        viridxa = [numpy.where(mo_occ[0][k]==0)[0] for k in range(nkpts)]
        viridxb = [numpy.where(mo_occ[1][k]==0)[0] for k in range(nkpts)]
        orboa = [mo_coeff[0][k][:,occidxa[k]] for k in range(nkpts)]
        orbob = [mo_coeff[1][k][:,occidxb[k]] for k in range(nkpts)]
        orbva = [mo_coeff[0][k][:,viridxa[k]] for k in range(nkpts)]
        orbvb = [mo_coeff[1][k][:,viridxb[k]] for k in range(nkpts)]

        e_ai_a = _get_eai(mo_energy[0], mo_occ[0])
        e_ai_b = _get_eai(mo_energy[1], mo_occ[1])
        hdiag = numpy.hstack([x.ravel() for x in (e_ai_a + e_ai_b)])
        hdiag = numpy.hstack((hdiag, hdiag))
        tot_x_a = sum(x.size for x in e_ai_a)
        tot_x_b = sum(x.size for x in e_ai_b)
        tot_x = tot_x_a + tot_x_b

        mem_now = lib.current_memory()[0]
        max_memory = max(2000, self.max_memory*.8-mem_now)
        vresp = _gen_uhf_response(mf, hermi=0, max_memory=max_memory)

        def vind(xys):
            nz = len(xys)
            x1s = [_unpack(x[:tot_x], mo_occ) for x in xys]
            y1s = [_unpack(x[tot_x:], mo_occ) for x in xys]
            dmvo = numpy.empty((2,nz,nkpts,nao,nao), dtype=numpy.complex128)
            for i in range(nz):
                xa, xb = x1s[i]
                ya, yb = y1s[i]
                for k in range(nkpts):
                    dmx = reduce(numpy.dot, (orbva[k], xa[k], orboa[k].T.conj()))
                    dmy = reduce(numpy.dot, (orboa[k], ya[k].T, orbva[k].T.conj()))
                    dmvo[0,i,k] = dmx + dmy  # AX + BY
                    dmx = reduce(numpy.dot, (orbvb[k], xb[k], orbob[k].T.conj()))
                    dmy = reduce(numpy.dot, (orbob[k], yb[k].T, orbvb[k].T.conj()))
                    dmvo[1,i,k] = dmx + dmy  # AX + BY

            v1ao = vresp(dmvo)
            v1s = []
            for i in range(nz):
                xa, xb = x1s[i]
                ya, yb = y1s[i]
                v1xsa = []
                v1xsb = []
                v1ysa = []
                v1ysb = []
                for k in range(nkpts):
                    v1xa = reduce(numpy.dot, (orbva[k].T.conj(), v1ao[0,i,k], orboa[k]))
                    v1xb = reduce(numpy.dot, (orbvb[k].T.conj(), v1ao[1,i,k], orbob[k]))
                    v1ya = reduce(numpy.dot, (orboa[k].T.conj(), v1ao[0,i,k], orbva[k])).T
                    v1yb = reduce(numpy.dot, (orbob[k].T.conj(), v1ao[1,i,k], orbvb[k])).T
                    v1xa+= e_ai_a[k] * xa[k]
                    v1xb+= e_ai_b[k] * xb[k]
                    v1ya+= e_ai_a[k] * ya[k]
                    v1yb+= e_ai_b[k] * yb[k]
                    v1xsa.append(v1xa.ravel())
                    v1xsb.append(v1xb.ravel())
                    v1ysa.append(-v1ya.ravel())
                    v1ysb.append(-v1yb.ravel())
                v1s += v1xsa + v1xsb + v1ysa + v1ysb
            return numpy.hstack(v1s).reshape(nz,-1)

        return vind, hdiag

    def init_guess(self, mf, nstates=None, wfnsym=None):
        x0 = TDA.init_guess(self, mf, nstates)
        y0 = numpy.zeros_like(x0)
        return numpy.hstack((x0,y0))

    def kernel(self, x0=None):
        '''TDHF diagonalization with non-Hermitian eigenvalue solver
        '''
        self.check_sanity()
        self.dump_flags()

        vind, hdiag = self.get_vind(self._scf)
        precond = self.get_precond(hdiag)
        if x0 is None:
            x0 = self.init_guess(self._scf, self.nstates)

        # We only need positive eigenvalues
        def pickeig(w, v, nroots, envs):
            realidx = numpy.where((abs(w.imag) < REAL_EIG_THRESHOLD) &
                                  (w.real > POSTIVE_EIG_THRESHOLD))[0]
            idx = realidx[w[realidx].real.argsort()]
            return w[idx].real, v[:,idx].real, idx

        self.converged, w, x1 = \
                lib.davidson_nosym1(vind, x0, precond,
                                    tol=self.conv_tol,
                                    nroots=self.nstates, lindep=self.lindep,
                                    max_space=self.max_space, pick=pickeig,
                                    verbose=self.verbose)

        mo_occ = self._scf.mo_occ
        e = []
        xy = []
        for i, z in enumerate(x1):
            xs, ys = z.reshape(2,-1)
            norm = lib.norm(xs)**2 - lib.norm(ys)**2
            if norm > 0:
                norm = 1/numpy.sqrt(norm)
                xs *= norm
                ys *= norm
                e.append(w[i])
                xy.append((_unpack(xs, mo_occ), _unpack(ys, mo_occ)))
        self.e = numpy.array(e)
        self.xy = xy
        return self.e, self.xy
RPA = TDHF

def _unpack(vo, mo_occ):
    nmo = mo_occ.shape[-1]
    nocca = numpy.sum(mo_occ[0] > 0, axis=1)
    noccb = numpy.sum(mo_occ[1] > 0, axis=1)
    za = []
    zb = []
    ip = 0
    for k, occ in enumerate(mo_occ[0]):
        no = numpy.count_nonzero(occ > 0)
        nv = occ.size - no
        za.append(vo[ip:ip+nv*no].reshape(nv,no))
        ip += nv * no

    for k, occ in enumerate(mo_occ[1]):
        no = numpy.count_nonzero(occ > 0)
        nv = occ.size - no
        zb.append(vo[ip:ip+nv*no].reshape(nv,no))
        ip += nv * no
    return za, zb


if __name__ == '__main__':
    from pyscf.pbc import gto
    from pyscf.pbc import scf
    from pyscf.pbc import df
    cell = gto.Cell()
    cell.unit = 'B'
    cell.atom = '''
    C  0.          0.          0.        
    C  1.68506879  1.68506879  1.68506879
    '''
    cell.a = '''
    0.          3.37013758  3.37013758
    3.37013758  0.          3.37013758
    3.37013758  3.37013758  0.
    '''

    cell.basis = 'gth-szv'
    cell.pseudo = 'gth-pade'
    cell.mesh = [37]*3
    cell.build()
    mf = scf.KUHF(cell, cell.make_kpts([2,1,1])).set(exxdiv=None)
#    mf.with_df = df.DF(cell, cell.make_kpts([2,1,1]))
#    mf.with_df.auxbasis = 'weigend'
#    mf.with_df._cderi = 'eri3d-df.h5'
#    mf.with_df.build(with_j3c=False)
    mf.run()

    td = TDA(mf)
    td.verbose = 5
    td.nstates = 5
    print(td.kernel()[0] * 27.2114)

    td = TDHF(mf)
    td.verbose = 5
    td.nstates = 5
    print(td.kernel()[0] * 27.2114)

    cell.spin = 2
    mf = scf.KUHF(cell, cell.make_kpts([2,1,1])).set(exxdiv=None)
    mf.run()

    td = TDA(mf)
    td.verbose = 5
    td.nstates = 5
    print(td.kernel()[0] * 27.2114)

    td = TDHF(mf)
    td.verbose = 5
    td.nstates = 5
    print(td.kernel()[0] * 27.2114)
