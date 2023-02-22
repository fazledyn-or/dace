PROGRAM enthalpy_flux_due_to_precipitation

    INTEGER, PARAMETER :: JPIM = SELECTED_INT_KIND(9)
    INTEGER, PARAMETER :: JPRB = SELECTED_REAL_KIND(13, 300)

    INTEGER(KIND=JPIM), PARAMETER  :: KLON = 100
    INTEGER(KIND=JPIM), PARAMETER  :: KLEV = 100

    INTEGER(KIND=JPIM)  :: KIDIA 
    INTEGER(KIND=JPIM)  :: KFDIA 
    REAL(KIND=JPRB)     :: RLVTT
    REAL(KIND=JPRB)     :: RLSTT
    REAL(KIND=JPRB)     :: PFPLSL(KLON,KLEV+1)
    REAL(KIND=JPRB)     :: PFPLSN(KLON,KLEV+1)
    REAL(KIND=JPRB)     :: PFHPSL(KLON,KLEV+1)
    REAL(KIND=JPRB)     :: PFHPSN(KLON,KLEV+1)

    CALL enthalpy_flux_due_to_precipitation_routine( &
        & KLON, KLEV, KIDIA, KFDIA, &
        & RLVTT, RLSTT, &
        & PFPLSL, PFPLSN, PFHPSL, PFHPSN)

END

SUBROUTINE enthalpy_flux_due_to_precipitation_routine( &
    & KLON, KLEV, KIDIA, KFDIA, &
    & RLVTT, RLSTT, &
    & PFPLSL, PFPLSN, PFHPSL, PFHPSN)

    INTEGER, PARAMETER :: JPIM = SELECTED_INT_KIND(9)
    INTEGER, PARAMETER :: JPRB = SELECTED_REAL_KIND(13, 300)

    INTEGER(KIND=JPIM)  :: KLON
    INTEGER(KIND=JPIM)  :: KLEV
    INTEGER(KIND=JPIM)  :: KIDIA 
    INTEGER(KIND=JPIM)  :: KFDIA 
    REAL(KIND=JPRB)     :: RLVTT
    REAL(KIND=JPRB)     :: RLSTT
    REAL(KIND=JPRB)     :: PFPLSL(KLON,KLEV+1)
    REAL(KIND=JPRB)     :: PFPLSN(KLON,KLEV+1)
    REAL(KIND=JPRB)     :: PFHPSL(KLON,KLEV+1)
    REAL(KIND=JPRB)     :: PFHPSN(KLON,KLEV+1)

    INTEGER(KIND=JPIM)  :: JK, JL

    DO JK=1,KLEV+1
        DO JL=KIDIA,KFDIA
            PFHPSL(JL,JK) = -RLVTT*PFPLSL(JL,JK)
            PFHPSN(JL,JK) = -RLSTT*PFPLSN(JL,JK)
        ENDDO
    ENDDO

END SUBROUTINE enthalpy_flux_due_to_precipitation_routine