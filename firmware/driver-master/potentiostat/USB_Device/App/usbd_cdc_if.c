/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : usbd_cdc_if.c
  * @version        : v3.0_Cube
  * @brief          : Usb device for Virtual Com Port.
  ******************************************************************************
  * @attention
  *
  * <h2><center>&copy; Copyright (c) 2023 STMicroelectronics.
  * All rights reserved.</center></h2>
  *
  * This software component is licensed by ST under Ultimate Liberty license
  * SLA0044, the "License"; You may not use this file except in compliance with
  * the License. You may obtain a copy of the License at:
  *                             www.st.com/SLA0044
  *
  ******************************************************************************
  */
/* USER CODE END Header */

/* Includes ------------------------------------------------------------------*/
#include "usbd_cdc_if.h"

/* USER CODE BEGIN INCLUDE */
#include "user.h"
#include "cmsis_os.h"
/* USER CODE END INCLUDE */

/* Private typedef -----------------------------------------------------------*/
/* Private define ------------------------------------------------------------*/
/* Private macro -------------------------------------------------------------*/

/* USER CODE BEGIN PV */
/* Private variables ---------------------------------------------------------*/

/* USER CODE END PV */

/** @addtogroup STM32_USB_OTG_DEVICE_LIBRARY
  * @brief Usb device library.
  * @{
  */

/** @addtogroup USBD_CDC_IF
  * @{
  */

/** @defgroup USBD_CDC_IF_Private_TypesDefinitions USBD_CDC_IF_Private_TypesDefinitions
  * @brief Private types.
  * @{
  */

/* USER CODE BEGIN PRIVATE_TYPES */

  extern uint32_t usb_rx_tmr;
  extern uint32_t rcv_ix;
  extern uint8_t serial_rx_buffer[SERIAL_RX_BUFFER_SIZE];

/* USER CODE END PRIVATE_TYPES */

/**
  * @}
  */

/** @defgroup USBD_CDC_IF_Private_Defines USBD_CDC_IF_Private_Defines
  * @brief Private defines.
  * @{
  */

/* USER CODE BEGIN PRIVATE_DEFINES */
/* Longest time to wait for the host to collect the previous transmission
 * before giving up and dropping the message. Bounds the transmit busy-wait
 * that used to spin forever when the host stopped reading, freezing the main
 * task until the watchdog reset the chip mid-experiment. */
#define CDC_TX_WAIT_MS 50u
/* USER CODE END PRIVATE_DEFINES */

/**
  * @}
  */

/** @defgroup USBD_CDC_IF_Private_Macros USBD_CDC_IF_Private_Macros
  * @brief Private macros.
  * @{
  */

/* USER CODE BEGIN PRIVATE_MACRO */

/* USER CODE END PRIVATE_MACRO */

/**
  * @}
  */

/** @defgroup USBD_CDC_IF_Private_Variables USBD_CDC_IF_Private_Variables
  * @brief Private variables.
  * @{
  */
/* Create buffer for reception and transmission           */
/* It's up to user to redefine and/or remove those define */
/** Received data over USB are stored in this buffer      */
uint8_t UserRxBufferFS[APP_RX_DATA_SIZE];

/** Data to send over USB CDC are stored in this buffer   */
uint8_t UserTxBufferFS[APP_TX_DATA_SIZE];

/* USER CODE BEGIN PRIVATE_VARIABLES */

/* Diagnostic counters, read by the host through the diagnostics query.
 * A healthy run leaves both at zero. */
uint32_t cdc_tx_dropped = 0;   /* messages discarded because the host stopped reading */
uint32_t cdc_rx_overflow = 0;  /* received frames discarded to protect the buffer */

/* Set after a transmit wait times out. While set, further transmissions are
 * dropped immediately (no waiting) so the measurement loop keeps its timing;
 * cleared as soon as the host collects the parked packet. */
static uint8_t cdc_tx_stalled = 0;

/* USER CODE END PRIVATE_VARIABLES */

/**
  * @}
  */

/** @defgroup USBD_CDC_IF_Exported_Variables USBD_CDC_IF_Exported_Variables
  * @brief Public variables.
  * @{
  */

extern USBD_HandleTypeDef hUsbDeviceFS;

/* USER CODE BEGIN EXPORTED_VARIABLES */

/* USER CODE END EXPORTED_VARIABLES */

/**
  * @}
  */

/** @defgroup USBD_CDC_IF_Private_FunctionPrototypes USBD_CDC_IF_Private_FunctionPrototypes
  * @brief Private functions declaration.
  * @{
  */

static int8_t CDC_Init_FS(void);
static int8_t CDC_DeInit_FS(void);
static int8_t CDC_Control_FS(uint8_t cmd, uint8_t* pbuf, uint16_t length);
static int8_t CDC_Receive_FS(uint8_t* pbuf, uint32_t *Len);
static int8_t CDC_TransmitCplt_FS(uint8_t *pbuf, uint32_t *Len, uint8_t epnum);

/* USER CODE BEGIN PRIVATE_FUNCTIONS_DECLARATION */
/* USER CODE END PRIVATE_FUNCTIONS_DECLARATION */

/**
  * @}
  */

USBD_CDC_ItfTypeDef USBD_Interface_fops_FS =
{
  CDC_Init_FS,
  CDC_DeInit_FS,
  CDC_Control_FS,
  CDC_Receive_FS,
  CDC_TransmitCplt_FS
};

/* Private functions ---------------------------------------------------------*/
/**
  * @brief  Initializes the CDC media low layer over the FS USB IP
  * @retval USBD_OK if all operations are OK else USBD_FAIL
  */
static int8_t CDC_Init_FS(void)
{
  /* USER CODE BEGIN 3 */

  /* Set Application Buffers */
  USBD_CDC_SetTxBuffer(&hUsbDeviceFS, UserTxBufferFS, 0);
  USBD_CDC_SetRxBuffer(&hUsbDeviceFS, UserRxBufferFS);
  return (USBD_OK);
  /* USER CODE END 3 */
}

/**
  * @brief  DeInitializes the CDC media low layer
  * @retval USBD_OK if all operations are OK else USBD_FAIL
  */
static int8_t CDC_DeInit_FS(void)
{
  /* USER CODE BEGIN 4 */
  return (USBD_OK);
  /* USER CODE END 4 */
}

/**
  * @brief  Manage the CDC class requests
  * @param  cmd: Command code
  * @param  pbuf: Buffer containing command data (request parameters)
  * @param  length: Number of data to be sent (in bytes)
  * @retval Result of the operation: USBD_OK if all operations are OK else USBD_FAIL
  */
static int8_t CDC_Control_FS(uint8_t cmd, uint8_t* pbuf, uint16_t length)
{
  /* USER CODE BEGIN 5 */
  switch(cmd)
  {
    case CDC_SEND_ENCAPSULATED_COMMAND:

    break;

    case CDC_GET_ENCAPSULATED_RESPONSE:

    break;

    case CDC_SET_COMM_FEATURE:

    break;

    case CDC_GET_COMM_FEATURE:

    break;

    case CDC_CLEAR_COMM_FEATURE:

    break;

  /*******************************************************************************/
  /* Line Coding Structure                                                       */
  /*-----------------------------------------------------------------------------*/
  /* Offset | Field       | Size | Value  | Description                          */
  /* 0      | dwDTERate   |   4  | Number |Data terminal rate, in bits per second*/
  /* 4      | bCharFormat |   1  | Number | Stop bits                            */
  /*                                        0 - 1 Stop bit                       */
  /*                                        1 - 1.5 Stop bits                    */
  /*                                        2 - 2 Stop bits                      */
  /* 5      | bParityType |  1   | Number | Parity                               */
  /*                                        0 - None                             */
  /*                                        1 - Odd                              */
  /*                                        2 - Even                             */
  /*                                        3 - Mark                             */
  /*                                        4 - Space                            */
  /* 6      | bDataBits  |   1   | Number Data bits (5, 6, 7, 8 or 16).          */
  /*******************************************************************************/
    case CDC_SET_LINE_CODING:

    break;

    case CDC_GET_LINE_CODING:

    break;

    case CDC_SET_CONTROL_LINE_STATE:

    break;

    case CDC_SEND_BREAK:

    break;

  default:
    break;
  }

  return (USBD_OK);
  /* USER CODE END 5 */
}

/**
  * @brief  Data received over USB OUT endpoint are sent over CDC interface
  *         through this function.
  *
  *         @note
  *         This function will issue a NAK packet on any OUT packet received on
  *         USB endpoint until exiting this function. If you exit this function
  *         before transfer is complete on CDC interface (ie. using DMA controller)
  *         it will result in receiving more data while previous ones are still
  *         not sent.
  *
  * @param  Buf: Buffer of data to be received
  * @param  Len: Number of data received (in bytes)
  * @retval Result of the operation: USBD_OK if all operations are OK else USBD_FAIL
  */
static int8_t CDC_Receive_FS(uint8_t* Buf, uint32_t *Len)
{
  /* USER CODE BEGIN 6 */

	/* Store received bytes in the frame buffer; USB delivers data in packets
	 * of up to 64 bytes, so one command frame may arrive in several pieces.
	 * The bounds check must run BEFORE the copy: the previous version copied
	 * first and could write past the end of the buffer whenever the main loop
	 * stopped draining it, corrupting whatever variables lived next to it.
	 * A frame that no longer fits is dropped whole and the buffer restarts. */
	if ((rcv_ix + *Len) <= SERIAL_RX_BUFFER_SIZE) {
		memcpy(&serial_rx_buffer[rcv_ix], Buf, *Len);
		rcv_ix = (rcv_ix + (*Len));
	} else {
		cdc_rx_overflow++;
		rcv_ix = 0;
	}

	usb_rx_tmr = GetTickCount();

	USBD_CDC_SetRxBuffer(&hUsbDeviceFS, &Buf[0]);
	USBD_CDC_ReceivePacket(&hUsbDeviceFS);
	return (USBD_OK);
  /* USER CODE END 6 */
}

/**
  * @brief  CDC_Transmit_FS
  *         Data to send over USB IN endpoint are sent over CDC interface
  *         through this function.
  *         @note
  *
  *
  * @param  Buf: Buffer of data to be sent
  * @param  Len: Number of data to be sent (in bytes)
  * @retval USBD_OK if all operations are OK else USBD_FAIL or USBD_BUSY
  */
/* Wait for the host to collect the previous transmission, but only briefly.
 * If the host has stopped reading (its receive path is full or the port is
 * gone), give up so the caller can drop the message instead of freezing the
 * instrument: the unbounded version of this wait is what froze the main task
 * during long CV runs until the watchdog reset the chip. After one timeout,
 * further calls fail immediately (no wait) so the DAC stepping cadence is
 * unaffected; normal operation resumes as soon as the host catches up. */
static uint8_t Wait_For_Tx_Ready(USBD_CDC_HandleTypeDef *hcdc)
{
	if (hcdc->TxState == 0) {
		cdc_tx_stalled = 0;
		return USBD_OK;
	}
	if (!cdc_tx_stalled) {
		uint32_t start = HAL_GetTick();
		while (hcdc->TxState != 0) {
			if ((HAL_GetTick() - start) > CDC_TX_WAIT_MS) {
				cdc_tx_stalled = 1;
				break;
			}
		}
		if (hcdc->TxState == 0) {
			cdc_tx_stalled = 0;
			return USBD_OK;
		}
	}
	cdc_tx_dropped++;
	return USBD_BUSY;
}

uint8_t CDC_Transmit_FS(uint8_t* Buf, uint32_t Len)
{
  //rcv_ix = 0;
  uint8_t result = USBD_OK;
  /* USER CODE BEGIN 7 */
  USBD_CDC_HandleTypeDef *hcdc = (USBD_CDC_HandleTypeDef*)hUsbDeviceFS.pClassData;
  if (hcdc == NULL) return USBD_FAIL;
  /* Each TransmitPacket below copies its bytes into the USB peripheral's own
   * packet memory before returning, so the caller's buffer is always safe to
   * reuse even when a wait times out and the message is abandoned early. */
  if (Wait_For_Tx_Ready(hcdc) != USBD_OK) return USBD_BUSY;
  if (Len <= 64) {
	  USBD_CDC_SetTxBuffer(&hUsbDeviceFS, Buf, Len);
	  result = USBD_CDC_TransmitPacket(&hUsbDeviceFS);
	  if (Wait_For_Tx_Ready(hcdc) != USBD_OK) return USBD_BUSY;
  } else {
	  uint16_t remainder = Len % 64;
	  uint16_t n_chunk = (Len - remainder) / 64;
	  //first chunks
	  for (int i=0;i<n_chunk;i++){
		  USBD_CDC_SetTxBuffer(&hUsbDeviceFS, Buf, 64);
		  result = USBD_CDC_TransmitPacket(&hUsbDeviceFS);
		  if (Wait_For_Tx_Ready(hcdc) != USBD_OK) return USBD_BUSY;
		  Buf+=64;
	  }
	  //then remainder
	  if (remainder > 0) {
		  USBD_CDC_SetTxBuffer(&hUsbDeviceFS, Buf, remainder);
		  result = USBD_CDC_TransmitPacket(&hUsbDeviceFS);
		  if (Wait_For_Tx_Ready(hcdc) != USBD_OK) return USBD_BUSY;
	  }
  }

  /* USER CODE END 7 */
  return result;
}

/**
  * @brief  CDC_TransmitCplt_FS
  *         Data transmitted callback
  *
  *         @note
  *         This function is IN transfer complete callback used to inform user that
  *         the submitted Data is successfully sent over USB.
  *
  * @param  Buf: Buffer of data to be received
  * @param  Len: Number of data received (in bytes)
  * @retval Result of the operation: USBD_OK if all operations are OK else USBD_FAIL
  */
static int8_t CDC_TransmitCplt_FS(uint8_t *Buf, uint32_t *Len, uint8_t epnum)
{
  uint8_t result = USBD_OK;
  /* USER CODE BEGIN 13 */
  UNUSED(Buf);
  UNUSED(Len);
  UNUSED(epnum);
  /* USER CODE END 13 */
  return result;
}

/* USER CODE BEGIN PRIVATE_FUNCTIONS_IMPLEMENTATION */

/* USER CODE END PRIVATE_FUNCTIONS_IMPLEMENTATION */

/**
  * @}
  */

/**
  * @}
  */
