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
 * WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT ARE
 * DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
 * ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
 * (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 * LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
 * ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
 * SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

/*!
 * @ingroup SPI_LAYER
 */
/**
 * @file    spi_driver.c
 * @brief   Definitions of spi_driver functions.
 */
#include <string.h>
#include "spi_driver.h"

static SPI_HandleTypeDef *spi_handler = NULL;

void FLASH_SPI_Init(SPI_HandleTypeDef *spi)
{
	spi_handler = spi;
}

void Delay_cycles(uint32_t delayTime)
{
    volatile uint32_t i = 0;
    for (i = 0; i < delayTime; ++i)
    {
        __asm("NOP"); /* delay */
    }
}

void SPI_Exchange(uint8_t *txBuffer, uint32_t txNumBytes, uint8_t *rxBuffer, uint32_t rxNumBytes, uint32_t dummyNumBytes)
{
	/* Select chip */
	HAL_GPIO_WritePin(FLASH_NSS_GPIO_Port, FLASH_NSS_Pin, 0);


	/* Send tx buffer */
	HAL_SPI_Transmit(spi_handler, txBuffer, txNumBytes, 100);

	/* Receive data and dummy bytes to buffer*/
	HAL_SPI_Receive(spi_handler, rxBuffer, (dummyNumBytes + rxNumBytes), 100);

	/* Skip dummy received bytes */
	if(dummyNumBytes)
		memmove(rxBuffer, (rxBuffer + dummyNumBytes), rxNumBytes);

	/* Deselect chip */
	HAL_GPIO_WritePin(FLASH_NSS_GPIO_Port, FLASH_NSS_Pin, 1);
}

void SPI_DualExchange(uint8_t standardSPINumBytes,
					  uint8_t *txBuffer,
					  uint32_t txNumBytes,
					  uint8_t *rxBuffer,
					  uint32_t rxNumBytes,
					  uint32_t dummyNumBytes)
{
};

void SPI_QuadExchange(uint8_t standardSPINumBytes,
					  uint8_t *txBuffer,
					  uint32_t txNumBytes,
					  uint8_t *rxBuffer,
					  uint32_t rxNumBytes,
					  uint32_t dummyNumBytes)
{
};

