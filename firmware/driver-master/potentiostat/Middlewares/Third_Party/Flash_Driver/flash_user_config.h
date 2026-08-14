/*
 * The Clear BSD License
 * Copyright (c) 2018 Adesto Technologies Corporation, Inc
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without modification,
 * are permitted (subject to the limitations in the disclaimer below) provided
 *  that the following conditions are met:
 *
 * o Redistributions of source code must retain the above copyright notice, this list
 *   of conditions and the following disclaimer.
 *
 * o Redistributions in binary form must reproduce the above copyright notice, this
 *   list of conditions and the following disclaimer in the documentation and/or
 *   other materials provided with the distribution.
 *
 * o Neither the name of the copyright holder nor the names of its
 *   contributors may be used to endorse or promote products derived from this
 *   software without specific prior written permission.
 *
 * NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY THIS LICENSE.
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
 * ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
 * (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 * LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
 * ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
 * SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

/*!
 * @ingroup USER_CONFIG Configuration Layer
 */
/**
 * @file    user_config.h
 * @brief   Project declarations exist here.
 */
#ifndef USER_CONFIG_H_
#define USER_CONFIG_H_


/* \=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\=\
 * @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 * ----------------- USER DEFINED PORTION ----------------
 * @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 * /=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/=/
 */

/*
 *
 * This portion of the code contains the various user definitions
 * and functions. These include the following:
 *
 * 1. MCU specific include files
 * 2. Part number
 * 3. SPI ports and pins
 * 3. User defined function calls to clear/set/init pins
 * 4. Board initialization function
 */


/*
 * @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 * --------------------- Part Number ---------------------
 * @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 */

/*! Definition of part number. */
#define PARTNO AT25SF321 /* <- Replace with the device being used. */
#define ALL 0 /* <- Replace with 0 if selecting a part above. */

#define MAX_PROGRAM_BYTES	256
#define ERASE_BLOCK_SIZE 	4096
#define NO_OF_SECTORS		1024

/*
 * List of supported parts:
 * RM331x
 * AT25XE512C
 * AT25XE011
 * AT25XE021A
 * AT25XE041B
 * AT25DN256
 * AT25DN512C
 * AT25DN011
 * AT25DF256
 * AT25DF512C
 * AT25DF011
 * AT25DF021A
 * AT25DF041B
 * AT25XV021A
 * AT25XV041B
 * AT45DB021E
 * AT45DB041E
 * AT45DB081E
 * AT45DB161E
 * AT45DB321E
 * AT45DB641E
 * AT45DQ161
 * AT45DQ321
 * AT25PE20
 * AT25PE40
 * AT25PE80
 * AT25PE16
 * AT25SF041
 * AT25SF081
 * AT25SF161
 * AT25SF321
 * AT25SF641
 * AT25SL321
 * AT25SL641
 * AT25SL128A
 * AT25DL081
 * AT25DL161
 * AT25DF081A
 * AT25DF321A
 * AT25DF641A
 * AT25QL321
 * AT25QL641
 * AT25QL128A
 * AT25QF641
 */


#define DELAY 5U

#endif
